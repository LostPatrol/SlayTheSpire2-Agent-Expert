"""Share repeated public prompt values without discarding gameplay information."""

from collections import Counter
from copy import deepcopy
from hashlib import sha256
import json


# Small values remain inline because reference syntax would add reading overhead.
MIN_SHARED_CHARACTERS = 96
# Large singleton values can be reused by the next retained conversation message.
MIN_REGISTER_CHARACTERS = 256
# At most four definition lookups separate any displayed value from its raw data.
MAX_REFERENCE_DEPTH = 4
# Short content identities reduce every reference; equality checks below resolve collisions.
REFERENCE_HASH_CHARACTERS = 8
# The wrapper prevents ordinary public dictionaries from being mistaken for references.
ENCODING = "public-shared-values-v1"
# The model reads references as data; no tool or code execution is required.
PROJECTION_GUIDE = (
    "A message with encoding=public-shared-values-v1 contains definitions and payload. "
    "Within payload or definitions, an object containing only $ref means the complete value "
    "at that key in this message's definitions or a preceding retained user message's definitions. "
    "Definitions are immutable within this conversation; references nest at most four levels. "
    "Only definitions reachable from the newest payload describe the current facts; unused "
    "earlier definitions and historical payloads are not current state. Always interpret the "
    "complete newest payload, including current amounts and indices, for this decision. "
    "Read them as shared data, without "
    "executing code. A sole $literal wraps an ordinary dictionary, not a reference. "
    "All array positions, card indices, live values, rule conditions and coverage limits remain "
    "present after expanding these references. Identical values share storage only; this never "
    "means two cards or entities have the same identity."
)


def _serialize(value: object) -> str:
    """Serialize public JSON deterministically for exact equality and size comparisons."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def serialize_projection(projection: dict) -> str:
    """Keep shared facts ahead of the changing action appendix without adding text."""
    # Only the outer key order changes; nested JSON keeps canonical serialization.
    order = ("encoding", "definitions", "payload", "current_action_reference")
    keys = [key for key in order if key in projection]
    keys.extend(sorted(set(projection) - set(keys)))
    return "{" + ",".join(_serialize(key) + ":" + _serialize(projection[key]) for key in keys) + "}"


def _walk(value: object):
    """Yield each value once per occurrence, including descendants of repeated objects."""
    yield value
    if isinstance(value, dict):
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def _references(value: object) -> Counter:
    """Count encoded reference uses without interpreting escaped literal dictionaries."""
    references = Counter()
    if isinstance(value, dict):
        if set(value) == {"$ref"}:
            references[value["$ref"]] += 1
        else:
            body = value["$literal"] if set(value) == {"$literal"} else value
            for item in body.values():
                references.update(_references(item))
    elif isinstance(value, list):
        for item in value:
            references.update(_references(item))
    return references


def compact_public(value: object, known_definitions: dict | None = None) -> dict:
    """Share exact values; return only new definitions without mutating retained ones."""
    known = known_definitions or {}
    counts = Counter()
    originals = {}
    for item in _walk(value):
        if not isinstance(item, (dict, list, str)):
            continue
        signature = _serialize(item)
        if len(signature) >= MIN_SHARED_CHARACTERS:
            counts[signature] += 1
            originals[signature] = item
    shared = {}
    known_signatures = {}
    for signature, count in counts.items():
        digest = sha256(signature.encode("utf-8")).hexdigest()
        identifier = "d_" + digest[:REFERENCE_HASH_CHARACTERS]
        if identifier in known:
            if identifier not in known_signatures:
                restored = expand_public({"encoding": ENCODING, "definitions": {},
                                          "payload": {"$ref": identifier}}, known)
                known_signatures[identifier] = _serialize(restored)
            if known_signatures[identifier] != signature:
                # A retained short-hash collision gets a full content hash instead.
                identifier = "d_" + digest
        if count > 1 or len(signature) >= MIN_REGISTER_CHARACTERS or identifier in known:
            shared[signature] = identifier
    # A hash collision must never silently merge two different public values.
    if len(set(shared.values())) != len(shared):
        shared = {signature: "d_" + sha256(signature.encode("utf-8")).hexdigest()
                  for signature in shared}

    def encode(item: object, *, definition_root: bool = False) -> object:
        """Replace only exact repeated values while escaping literal marker keys."""
        if isinstance(item, (dict, list, str)) and not definition_root:
            identifier = shared.get(_serialize(item))
            if identifier is not None:
                return {"$ref": identifier}
        if isinstance(item, dict):
            body = {key: encode(child) for key, child in item.items()}
            return {"$literal": body} if set(body) in ({"$ref"}, {"$literal"}) else body
        if isinstance(item, list):
            return [encode(child) for child in item]
        return item

    # Keep the public payload's top-level sections visible instead of referring its root.
    payload = encode(value, definition_root=True)
    definitions = {identifier: encode(originals[signature], definition_root=True)
                   for signature, identifier in shared.items() if identifier not in known}
    combined = {**known, **definitions}

    def limit_depth(item: object, remaining: int) -> object:
        """Inline distant references so reading a value never requires a long lookup chain."""
        if isinstance(item, dict):
            if set(item) == {"$ref"}:
                body = combined[item["$ref"]]
                if reference_depth(body) >= remaining:
                    return limit_depth(body, remaining)
                return item
            if set(item) == {"$literal"}:
                return {"$literal": {key: limit_depth(child, remaining)
                                     for key, child in item["$literal"].items()}}
            return {key: limit_depth(child, remaining) for key, child in item.items()}
        if isinstance(item, list):
            return [limit_depth(child, remaining) for child in item]
        return item

    depth_cache = {}

    def reference_depth(item: object) -> int:
        """Measure lookup depth using the already encoded acyclic definition graph."""
        if isinstance(item, dict):
            if set(item) == {"$ref"}:
                identifier = item["$ref"]
                if identifier not in depth_cache:
                    depth_cache[identifier] = 1 + reference_depth(combined[identifier])
                return depth_cache[identifier]
            body = item["$literal"] if set(item) == {"$literal"} else item
            return max((reference_depth(child) for child in body.values()), default=0)
        if isinstance(item, list):
            return max((reference_depth(child) for child in item), default=0)
        return 0

    # Smaller raw values are descendants, so their bodies can be finalized first.
    for signature, identifier in sorted(shared.items(), key=lambda pair: len(pair[0])):
        if identifier in definitions:
            definitions[identifier] = limit_depth(definitions[identifier], MAX_REFERENCE_DEPTH - 1)
            combined[identifier] = definitions[identifier]
            depth_cache.clear()
    reachable = set(_references(payload))
    pending = list(reachable)
    while pending:
        identifier = pending.pop()
        for child in _references(combined[identifier]):
            if child not in reachable:
                reachable.add(child)
                pending.append(child)
    definitions = {key: item for key, item in definitions.items() if key in reachable}
    return {"encoding": ENCODING, "definitions": dict(sorted(definitions.items())), "payload": payload}


def expand_public(projection: dict, known_definitions: dict | None = None) -> object:
    """Restore the original JSON for semantic-preservation tests and local evidence."""
    if projection.get("encoding") != ENCODING:
        raise ValueError("Unknown public projection encoding.")
    definitions = {**(known_definitions or {}), **projection["definitions"]}

    def expand(item: object, ancestors: frozenset = frozenset()) -> object:
        """Resolve a shared value without treating literal public markers as control."""
        if isinstance(item, dict):
            if set(item) == {"$ref"}:
                identifier = item["$ref"]
                if identifier in ancestors:
                    raise ValueError("Cyclic public projection reference.")
                return expand(definitions[identifier], ancestors | {identifier})
            body = item["$literal"] if set(item) == {"$literal"} else item
            return {key: expand(child, ancestors) for key, child in body.items()}
        if isinstance(item, list):
            return [expand(child, ancestors) for child in item]
        return deepcopy(item)

    return expand(projection["payload"])
