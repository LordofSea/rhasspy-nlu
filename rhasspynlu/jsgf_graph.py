"""Utilities to convert JSGF sentences to directed graphs."""
import base64
import gzip
import io
import math
import typing
from dataclasses import dataclass
from pathlib import Path

import networkx as nx

from .const import IntentsType, ReplacementsType, SentencesType
from .ini_jsgf import get_intent_counts, split_rules
from .jsgf import (
    Expression,
    RuleReference,
    Sentence,
    Sequence,
    SequenceType,
    SlotReference,
    Substitutable,
    Taggable,
    Word,
)
from .slots import split_slot_args

# -----------------------------------------------------------------------------


def expression_to_graph(
    expression: Expression,
    graph: nx.DiGraph,
    source_state: int,
    replacements: typing.Optional[ReplacementsType] = None,
    empty_substitution: int = 0,
    grammar_name: typing.Optional[str] = None,
    count_dict: typing.Optional[typing.Dict[Expression, int]] = None,
    rule_grammar: str = "",
    expand_slots: bool = True,
) -> int:
    """Insert JSGF expression into a graph. Return final state."""
    replacements = replacements or {}

    # Handle sequence substitution
    if isinstance(expression, Substitutable) and expression.substitution:
        # Ensure everything downstream outputs nothing
        empty_substitution += 1

    # Handle tag begin
    if isinstance(expression, Taggable) and expression.tag:
        # Begin tag
        next_state = len(graph)
        tag = expression.tag.tag_text
        olabel = f"__begin__{tag}"
        label = f":{olabel}"
        graph.add_edge(
            source_state, next_state, ilabel="", olabel=maybe_pack(olabel), label=label
        )
        source_state = next_state

        if expression.tag.substitution:
            # Ensure everything downstream outputs nothing
            empty_substitution += 1

    # Handle converters begin
    begin_converters: typing.List[str] = []
    if isinstance(expression, Taggable) and expression.tag:
        begin_converters.extend(reversed(expression.tag.converters))

    if isinstance(expression, Substitutable) and expression.converters:
        begin_converters.extend(reversed(expression.converters))

    # Create begin transitions for each converter (in reverse order)
    for converter_name in begin_converters:
        next_state = len(graph)
        olabel = f"__convert__{converter_name}"
        label = f"!{olabel}"
        graph.add_edge(
            source_state, next_state, ilabel="", olabel=maybe_pack(olabel), label=label
        )
        source_state = next_state

    if isinstance(expression, Sequence):
        # Group, optional, or alternative
        seq: Sequence = expression
        if seq.type == SequenceType.ALTERNATIVE:
            # Optional or alternative
            final_states = []
            for item in seq.items:
                # Branch alternatives from source state
                next_state = expression_to_graph(
                    item,
                    graph,
                    source_state,
                    replacements=replacements,
                    empty_substitution=empty_substitution,
                    grammar_name=grammar_name,
                    count_dict=count_dict,
                    rule_grammar=rule_grammar,
                    expand_slots=expand_slots,
                )
                final_states.append(next_state)

            # Connect all paths to final state
            next_state = len(graph)
            for final_state in final_states:
                graph.add_edge(final_state, next_state, ilabel="", olabel="", label="")

            source_state = next_state
        else:
            # Group
            next_state = source_state
            for item in seq.items:
                # Create sequence of states
                next_state = expression_to_graph(
                    item,
                    graph,
                    next_state,
                    replacements=replacements,
                    empty_substitution=empty_substitution,
                    grammar_name=grammar_name,
                    count_dict=count_dict,
                    rule_grammar=rule_grammar,
                    expand_slots=expand_slots,
                )

            source_state = next_state
    elif isinstance(expression, Word):
        # State for single word
        word: Word = expression
        next_state = len(graph)
        graph.add_node(next_state, word=word.text)

        if (word.substitution is None) and (empty_substitution <= 0):
            # Single word input/output
            graph.add_edge(
                source_state,
                next_state,
                ilabel=word.text,
                olabel=word.text,
                label=word.text,
            )
            source_state = next_state
        else:
            # Loading edge
            graph.add_edge(
                source_state,
                next_state,
                ilabel=word.text,
                olabel="",
                label=f"{word.text}:",
            )

            source_state = next_state

            # Add word output(s)
            olabels = [word.text] if (word.substitution is None) else word.substitution
            if empty_substitution <= 0:
                source_state = add_substitution(graph, olabels, source_state)
    elif isinstance(expression, RuleReference):
        # Reference to a local or remote rule
        rule_ref: RuleReference = expression
        if rule_ref.grammar_name:
            # Fully resolved rule name
            rule_name = f"{rule_ref.grammar_name}.{rule_ref.rule_name}"
            rule_grammar = rule_ref.grammar_name
        elif rule_grammar:
            # Nested rule
            rule_name = f"{rule_grammar}.{rule_ref.rule_name}"
        elif grammar_name:
            # Local rule
            rule_name = f"{grammar_name}.{rule_ref.rule_name}"
            rule_grammar = grammar_name
        else:
            # Unresolved rule name
            rule_name = rule_ref.rule_name

        # Surround with <>
        rule_name_brackets = f"<{rule_name}>"
        rule_replacements = replacements.get(rule_name_brackets)
        assert rule_replacements, f"Missing rule {rule_name}"

        rule_body = next(iter(rule_replacements))
        assert isinstance(rule_body, Sentence), f"Invalid rule {rule_name}: {rule_body}"
        source_state = expression_to_graph(
            rule_body,
            graph,
            source_state,
            replacements=replacements,
            empty_substitution=empty_substitution,
            grammar_name=grammar_name,
            count_dict=count_dict,
            rule_grammar=rule_grammar,
            expand_slots=expand_slots,
        )

    elif isinstance(expression, SlotReference):
        # Reference to slot values
        slot_ref: SlotReference = expression

        # Prefix with $
        slot_name = "$" + slot_ref.slot_name

        if expand_slots:
            slot_values = replacements.get(slot_name)
            assert slot_values, f"Missing slot {slot_name}"

            # Interpret as alternative
            slot_seq = Sequence(type=SequenceType.ALTERNATIVE, items=list(slot_values))
            source_state = expression_to_graph(
                slot_seq,
                graph,
                source_state,
                replacements=replacements,
                empty_substitution=(
                    empty_substitution + (1 if slot_ref.substitution else 0)
                ),
                grammar_name=grammar_name,
                count_dict=count_dict,
                rule_grammar=rule_grammar,
                expand_slots=expand_slots,
            )

        # Emit __source__ with slot name (no arguments)
        slot_name_noargs = split_slot_args(slot_ref.slot_name)[0]
        next_state = len(graph)
        olabel = f"__source__{slot_name_noargs}"
        graph.add_edge(
            source_state, next_state, ilabel="", olabel=olabel, label=maybe_pack(olabel)
        )
        source_state = next_state

    # Handle sequence substitution
    if isinstance(expression, Substitutable) and expression.substitution:
        # Output substituted word(s)
        empty_substitution -= 1
        if empty_substitution <= 0:
            source_state = add_substitution(
                graph, expression.substitution, source_state
            )

    # Handle converters end
    end_converters: typing.List[str] = []
    if isinstance(expression, Substitutable) and expression.converters:
        end_converters.extend(expression.converters)

    if isinstance(expression, Taggable) and expression.tag:
        end_converters.extend(expression.tag.converters)

    # Handle tag end
    if isinstance(expression, Taggable) and expression.tag:
        # Handle tag substitution
        if expression.tag.substitution:
            # Output substituted word(s)
            source_state = add_substitution(
                graph, expression.tag.substitution, source_state
            )

        # Create end transitions for each converter
        for converter_name in end_converters:
            next_state = len(graph)
            olabel = f"__converted__{converter_name}"
            label = f"!{olabel}"
            graph.add_edge(
                source_state,
                next_state,
                ilabel="",
                olabel=maybe_pack(olabel),
                label=label,
            )
            source_state = next_state

        # End tag
        next_state = len(graph)
        tag = expression.tag.tag_text
        olabel = f"__end__{tag}"
        label = f":{olabel}"
        graph.add_edge(
            source_state, next_state, ilabel="", olabel=maybe_pack(olabel), label=label
        )
        source_state = next_state
    else:
        # Create end transitions for each converter
        for converter_name in end_converters:
            next_state = len(graph)
            olabel = f"__converted__{converter_name}"
            label = f"!{olabel}"
            graph.add_edge(
                source_state,
                next_state,
                ilabel="",
                olabel=maybe_pack(olabel),
                label=label,
            )
            source_state = next_state

    return source_state


def add_substitution(
    graph: nx.DiGraph,
    substitution: typing.Union[str, typing.List[str]],
    source_state: int,
) -> int:
    """Add substitution token sequence to graph."""
    if isinstance(substitution, str):
        substitution = [substitution]

    for olabel in substitution:
        next_state = len(graph)
        graph.add_edge(
            source_state,
            next_state,
            ilabel="",
            olabel=maybe_pack(olabel),
            label=f":{olabel}",
        )

        source_state = next_state

    return source_state


def maybe_pack(olabel: str) -> str:
    """Pack output label as base64 if it contains whitespace."""
    if " " in olabel:
        return "__unpack__" + base64.encodebytes(olabel.encode()).decode().strip()

    return olabel


# -----------------------------------------------------------------------------


def intents_to_graph(
    intents: IntentsType,
    replacements: typing.Optional[ReplacementsType] = None,
    add_intent_weights: bool = True,
    exclude_slots_from_counts: bool = True,
) -> nx.DiGraph:
    """Convert sentences/rules grouped by intent into a directed graph."""
    sentences, replacements = split_rules(intents, replacements)
    return sentences_to_graph(
        sentences,
        replacements=replacements,
        add_intent_weights=add_intent_weights,
        exclude_slots_from_counts=exclude_slots_from_counts,
    )


def sentences_to_graph(
    sentences: SentencesType,
    replacements: typing.Optional[ReplacementsType] = None,
    add_intent_weights: bool = True,
    exclude_slots_from_counts: bool = True,
    expand_slots: bool = True,
) -> nx.DiGraph:
    """Convert sentences grouped by intent into a directed graph."""
    num_intents = len(sentences)
    intent_weights: typing.Dict[str, float] = {}
    count_dict: typing.Optional[typing.Dict[Expression, int]] = None

    if add_intent_weights:
        # Count number of posssible sentences per intent
        intent_counts = get_intent_counts(
            sentences,
            replacements,
            exclude_slots=exclude_slots_from_counts,
            count_dict=count_dict,
        )

        # Fix zero counts
        for intent_name in intent_counts:
            intent_counts[intent_name] = max(intent_counts[intent_name], 1)

        num_sentences_lcm = lcm(*intent_counts.values())
        intent_weights = {
            intent_name: (
                num_sentences_lcm // max(intent_counts.get(intent_name, 1), 1)
            )
            for intent_name in sentences
        }

        # Normalize
        weight_sum = max(sum(intent_weights.values()), 1)
        for intent_name in intent_weights:
            intent_weights[intent_name] /= weight_sum
    else:
        intent_counts = {}

    # Create initial graph
    graph: nx.DiGraph = nx.DiGraph()
    root_state: int = 0
    graph.add_node(root_state, start=True)
    final_states: typing.List[int] = []

    for intent_name, intent_sentences in sentences.items():
        # Branch off for each intent from start state
        intent_state = len(graph)
        olabel = f"__label__{intent_name}"
        label = f":{olabel}"

        edge_kwargs: typing.Dict[str, typing.Any] = {}
        if add_intent_weights and (num_intents > 1):
            edge_kwargs["sentence_count"] = intent_counts.get(intent_name, 1)
            edge_kwargs["weight"] = intent_weights.get(intent_name, 0)

        graph.add_edge(
            root_state,
            intent_state,
            ilabel="",
            olabel=olabel,
            label=label,
            **edge_kwargs,
        )

        for sentence in intent_sentences:
            # Insert all sentences for this intent
            next_state = expression_to_graph(  # type: ignore
                sentence,
                graph,
                intent_state,
                replacements=replacements,
                grammar_name=intent_name,
                count_dict=count_dict,
                expand_slots=expand_slots,
            )
            final_states.append(next_state)

    # Create final state and join all sentences to it
    final_state = len(graph)
    graph.add_node(final_state, final=True)

    for next_state in final_states:
        graph.add_edge(next_state, final_state, ilabel="", olabel="", label="")

    return graph


# -----------------------------------------------------------------------------


def graph_to_json(graph: nx.DiGraph) -> typing.Dict[str, typing.Any]:
    """Convert to dict suitable for JSON serialization."""
    return nx.readwrite.json_graph.node_link_data(graph)


def json_to_graph(json_dict: typing.Dict[str, typing.Any]) -> nx.DiGraph:
    """Convert from deserialized JSON dict to graph."""
    return nx.readwrite.json_graph.node_link_graph(json_dict)


def graph_to_gzip_pickle(graph: nx.DiGraph, out_file: typing.BinaryIO):
    """Convert to binary gzip pickle format."""
    with gzip.GzipFile(fileobj=out_file, mode="wb") as graph_gzip:
        nx.readwrite.gpickle.write_gpickle(graph, graph_gzip)


def gzip_pickle_to_graph(in_file: typing.BinaryIO) -> nx.DiGraph:
    """Convert from binary gzip pickle format."""
    with gzip.GzipFile(fileobj=in_file, mode="rb") as graph_gzip:
        return nx.readwrite.gpickle.read_gpickle(graph_gzip)


# -----------------------------------------------------------------------------



def lcm(*nums: int) -> int:
    """Returns the least common multiple of the given integers"""
    if nums:
        nums_lcm = nums[0]
        for n in nums[1:]:
            nums_lcm = (nums_lcm * n) // math.gcd(nums_lcm, n)

        return nums_lcm

    return 1


# -----------------------------------------------------------------------------


def get_start_end_nodes(
    graph: nx.DiGraph,
) -> typing.Tuple[typing.Optional[int], typing.Optional[int]]:
    """Return start/end nodes in graph"""
    n_data = graph.nodes(data=True)
    start_node = None
    end_node = None

    for node, data in n_data:
        if data.get("start", False):
            start_node = node
        elif data.get("final", False):
            end_node = node

        if (start_node is not None) and (end_node is not None):
            break

    return (start_node, end_node)
