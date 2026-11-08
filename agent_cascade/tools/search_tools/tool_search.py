# Copyright 2023 The Qwen team, Alibaba Group. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""``tool_search`` — deferred MCP tool discovery.

Core tools are always offered; MCP (dynamic) tools are *deferred* behind this fixed
``tool_search`` tool. Searching unlocks a *subset* of the deferred tools for the calling
instance: the matched names are appended to the instance's ``_discovered_mcp_tools`` tail
(see ``engine/helpers.py`` Seam A) and become part of the ``functions=`` payload from the
next call on. Undiscovered deferred tools are never offered, which is the token win.

The ``description`` is a FIXED class attribute (``dna.py`` ``TOOL_METADATA``) so the tool
itself never shifts in the prompt prefix as MCP servers connect or disconnect.

Ranking is a self-contained Okapi BM25 (``k1=1.2, b=0.75``, name-weighted, local-name
bonus), a port of pi's ``durable/src/search.ts`` adapted to AC's ``server-tool`` naming —
the name-boost and local-name fields split on ``-`` (``mcp_manager.py`` naming rule), not
pi's ``__``. It deliberately avoids ``rank_bm25`` (an optional ``[rag]`` extra) and
``keyword_search`` (coupled to RAG ``Record`` docs): ``tool_search`` is a core, always-on
tool and must not require a soft dependency.
"""

import json
import math
import re
from collections import Counter
from typing import Any, Dict, List, Optional

from agent_cascade.log import logger
from agent_cascade.prompts.dna import TOOL_METADATA
from agent_cascade.tools.base import BaseTool, register_tool

# ── JSON result envelope (F2: AC tools return a str; the re-derivation parse target) ──
# ``call()`` returns human text followed by one trailing line that is valid JSON.
# ``pool/session_io.py`` re-derives ``_discovered_mcp_tools`` from these envelope lines on
# session load, so a prose-parsing dependency is avoided. These names are the single
# source of truth — import them (do not hardcode) in tests and in session re-derivation.
TOOL_SEARCH_ENVELOPE_KEY = 'tool_search'
DISCOVERED_KEY = 'loaded'

# ── Catalog mode (empty query → read-only listing of ALL deferred MCP tools) ──
# The catalog is a bounded tool RESULT (conversation text), NOT part of the ``functions``
# array, so the deferral feature's KV-cache prefix stability is fully preserved. These keys
# extend the envelope alongside DISCOVERED_KEY (which stays present in EVERY mode); session
# re-derivation reads only DISCOVERED_KEY, so it remains compatible. Names are the single
# source of truth — import (do not hardcode) them in tests.
CATALOG_KEY = 'catalog'
COUNT_KEY = 'count'
TRUNCATED_KEY = 'truncated'
# Total tools shown in a catalog listing before it is marked truncated.
_CATALOG_MAX = 50
# Max chars of each tool's one-line (first-line) description in the catalog listing.
_CATALOG_DESC_LEN = 120

# ── BM25 parameters (port of pi durable/src/search.ts) ──
_K1 = 1.2
_B = 0.75
# A query term present in a tool's NAME counts this many times its IDF, so a name hit
# beats any number of mentions in the description. pi's NAMESPACE_WEIGHT is not used: AC
# MCP names embed the server name inline (``server-tool``), so it is already in name terms.
_NAME_WEIGHT = 3.0

# AC MCP tool naming separator: ``server_name + '-' + tool.name`` (mcp_manager.py). pi
# uses ``__``; the local-name bonus and exact-name match must key on ``-`` for AC.
_NAME_SEP = '-'

# English stop words (subset of pi's), so short queries are not dominated by function
# words. Kept in-module (no dependency on keyword_search's larger list).
_STOP_WORDS = frozenset(
    'a an and are as at be by for from in is it of on or that this to with the our your '
    'we you they them his her its their'.split())


def _stem(term: str) -> str:
    """Naive singularizer (port of pi ``stem``): ``issues`` -> ``issue``, ``searches`` -> ``search``."""
    if len(term) > 4 and term.endswith('ies'):
        return term[:-3] + 'y'
    if len(term) > 4 and re.search(r'(ches|shes|sses|xes|zes)$', term):
        return term[:-2]
    if len(term) > 3 and term.endswith('s') and not term.endswith('ss'):
        return term[:-1]
    return term


def _tokenize(text: str) -> List[str]:
    """Lowercase, split camelCase/PascalCase and any non-alphanumeric (incl. ``-`` and ``_``)
    into words, drop stop words, and stem ASCII words. MCP names are ASCII so this ASCII
    tokenizer is sufficient (pi additionally runs a word segmenter for CJK, which AC does
    not need here)."""
    normalized = text
    normalized = re.sub(r'([a-z0-9])([A-Z])', r'\1 \2', normalized)
    normalized = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1 \2', normalized)
    normalized = re.sub(r'[^A-Za-z0-9]+', ' ', normalized).lower()
    out = []
    for word in normalized.split():
        if not word or word in _STOP_WORDS:
            continue
        out.append(_stem(word) if word.isascii() and word.isalnum() else word)
    return out


def _normalize_name(name: str) -> str:
    """Fold a tool name to a comparable form: lowercase, separators (``-``/``_``/space)
    collapsed to ``_`` so ``dev-radius`` matches ``dev_radius``."""
    return re.sub(r'[^a-z0-9]+', '_', name.strip().lower()).strip('_')


def rank_deferred_tools(query: str, documents: List[Dict[str, str]], limit: int) -> List[str]:
    """Okapi BM25 (``k1=1.2, b=0.75``) over ad-hoc ``{'name','text'}`` docs; best-match names
    first, ties keep input order.

    - A term in the tool NAME scores ``NAME_WEIGHT`` times its IDF (beats any text mentions).
    - A tool whose whole local name (after the last ``-``) is covered by the query gets a
      bonus worth one more match of every query term.
    - A query equal to the whole name or its local part ranks that tool first, before coverage.
    - Only ``text`` is length-normalized, so a long name does not penalize a tool.
    """
    query_terms = list(dict.fromkeys(_tokenize(query)))
    if not query_terms or not documents or limit <= 0:
        return []

    query_term_set = set(query_terms)
    indexed = []
    for doc in documents:
        text_counts = Counter(_tokenize(doc['text']))
        name_terms = set(_tokenize(doc['name']))
        local_terms = list(dict.fromkeys(_tokenize(doc['name'].split(_NAME_SEP)[-1])))
        indexed.append({
            'name': doc['name'],
            'text_counts': text_counts,
            'text_length': sum(text_counts.values()),
            'name_terms': name_terms,
            'local_terms': local_terms,
        })

    n_docs = len(indexed)
    avg_length = sum(x['text_length'] for x in indexed) / n_docs or 1.0

    idf = {}
    for term in query_terms:
        freq = sum(1 for x in indexed if term in x['name_terms'] or term in x['text_counts'])
        idf[term] = math.log(1.0 + (n_docs - freq + 0.5) / (freq + 0.5))
    local_name_bonus = sum(idf.values())

    exact_query = _normalize_name(query)
    matches = []
    for x in indexed:
        length_norm = _K1 * (1.0 - _B + (_B * x['text_length']) / avg_length)
        score = 0.0
        covered = 0
        for term in query_terms:
            count = x['text_counts'].get(term, 0)
            weight = ((_NAME_WEIGHT if term in x['name_terms'] else 0.0)
                      + (count * (_K1 + 1.0)) / (count + length_norm))
            if weight > 0:
                covered += 1
            score += idf[term] * weight
        if score == 0:
            continue
        # local-name bonus: the tool's full local name (after the last '-') is in the query.
        if x['local_terms'] and all(term in query_term_set for term in x['local_terms']):
            score += local_name_bonus
        full_norm = _normalize_name(x['name'])
        local_norm = _normalize_name(x['name'].split(_NAME_SEP)[-1])
        exact = exact_query == full_norm or exact_query == local_norm
        matches.append({'name': x['name'], 'score': score, 'covered': covered, 'exact': exact})

    matches.sort(key=lambda m: (not m['exact'], -m['covered'], -m['score']))
    return [m['name'] for m in matches[:limit]]


@register_tool('tool_search')
class ToolSearch(BaseTool):
    # Fixed class-attribute description (never interpolates the deferred-tool list) so the
    # tool does not shift in the prompt prefix as MCP servers connect or disconnect.
    description = TOOL_METADATA['tool_search']['description']
    parameters = TOOL_METADATA['tool_search']['parameters']

    # Default number of matches to unlock when the caller omits ``limit``.
    DEFAULT_LIMIT = 5

    def _resolve_context(self, agent_obj: Any, instance_name: str):
        """Resolve (instance, template) from the dispatch context.

        ``agent_obj`` is the engine (``tool_dispatcher.execute_tool`` passes
        ``agent_obj=self.engine``), which exposes ``.pool``; the instance is looked up by
        name and its template via ``pool.get_template``. Returns ``(None, None)`` when any
        link is missing so an unwired call degrades gracefully instead of crashing.
        """
        pool = getattr(agent_obj, 'pool', None)
        if pool is None or not instance_name:
            return None, None
        get_instance = getattr(pool, 'get_instance', None)
        instance = get_instance(instance_name) if callable(get_instance) else None
        if instance is None:
            return None, None
        get_template = getattr(pool, 'get_template', None)
        template = get_template(getattr(instance, 'agent_class', None)) if callable(get_template) else None
        return instance, template

    def _candidate_docs(self, func_map: Dict[str, Any], discovered: List[str]) -> List[Dict[str, str]]:
        """Build ``{'name','text'}`` docs for the not-yet-unlocked deferred (MCP) tools.

        Deferred membership is ``MCPManager.is_mcp_tool`` (single source of truth); core
        tools and ``tool_search`` itself are never candidates. ``text`` is the description
        plus parameter names/descriptions (mirrors pi ``tool-search.ts``)."""
        from agent_cascade.tools.mcp_manager import MCPManager
        discovered_set = set(discovered)
        docs = []
        for name, tool in func_map.items():
            if name in discovered_set or not MCPManager.is_mcp_tool(name):
                continue
            function = getattr(tool, 'function', None) or {}
            parts = [function.get('description', '') or '']
            params = function.get('parameters', None)
            properties = params.get('properties', {}) if isinstance(params, dict) else {}
            for p_name, p_schema in properties.items():
                parts.append(p_name)
                if isinstance(p_schema, dict):
                    parts.append(p_schema.get('description', '') or '')
            docs.append({'name': name, 'text': ' '.join(parts)})
        return docs

    def _catalog(self, func_map: Dict[str, Any]) -> List[Dict[str, str]]:
        """Build ``{'name','server','desc'}`` entries for ALL MCP tools in ``func_map`` —
        discovered or not — so the agent sees the full landscape. Membership predicate is
        ``MCPManager.is_mcp_tool`` (same single source of truth as ``_candidate_docs``).

        ``server`` is the segment before the first ``-`` (AC MCP naming is ``server-tool``;
        a name with no ``-`` yields the whole name). ``desc`` is the description's first line
        truncated to ``_CATALOG_DESC_LEN`` chars (``…`` appended when cut); empty desc → empty
        string, and the message formatter omits the ``: desc`` suffix."""
        from agent_cascade.tools.mcp_manager import MCPManager
        entries = []
        for name, tool in func_map.items():
            if not MCPManager.is_mcp_tool(name):
                continue
            function = getattr(tool, 'function', None) or {}
            description = function.get('description', '') or ''
            first_line = description.splitlines()[0].strip() if description else ''
            desc = (first_line if len(first_line) <= _CATALOG_DESC_LEN
                    else first_line[:_CATALOG_DESC_LEN] + '…')
            entries.append({'name': name, 'server': name.split(_NAME_SEP)[0], 'desc': desc})
        return entries

    def _catalog_message(self, entries: List[Dict[str, str]], total_count: int,
                         truncated: bool) -> str:
        """Render the grouped, human-facing catalog text. ``entries`` is the (capped) entry
        list; ``total_count`` is the FULL tool count (so the header reflects what was found,
        not merely what fits). Grouping preserves first-seen server order and, within a
        server, ``function_map`` order — ``dict`` insertion order carries both for free."""
        if not entries:
            return 'No MCP tools are currently enabled.'
        grouped: Dict[str, List[Dict[str, str]]] = {}
        for entry in entries:
            grouped.setdefault(entry['server'], []).append(entry)
        lines = [f'Available MCP tools ({total_count} across {len(grouped)} server(s)):']
        for server, tools in grouped.items():
            lines.append(f'  {server}:')
            for tool in tools:
                name = tool['name']
                desc = tool['desc']
                suffix = f': {desc}' if desc else ''
                lines.append(f'    - {name}{suffix}')
        if truncated:
            lines.append(f'[+{total_count - _CATALOG_MAX} more not shown — use a targeted '
                         'tool_search query to unlock specific tools]')
        return '\n'.join(lines)

    def call(self, params: Any, **kwargs) -> str:
        """Search the deferred MCP tools' metadata and unlock the matches for this instance.

        On a hit the ranked names are appended (dedup, insertion order) to
        ``instance._discovered_mcp_tools`` so Seam A offers them from the next call. Returns
        human-facing text plus a trailing ``{"tool_search": {"loaded": [...]}}`` envelope line
        (F2) that ``session_io`` re-derives on load. GIL-atomic append only — no lock (F5).
        """
        params = self._verify_json_format_args(params)
        query = params.get('query', '') or ''
        try:
            limit = int(params.get('limit', self.DEFAULT_LIMIT))
        except (TypeError, ValueError):
            limit = self.DEFAULT_LIMIT

        instance, template = self._resolve_context(kwargs.get('agent_obj'),
                                                    kwargs.get('agent_instance_name') or kwargs.get('agent_name'))
        if instance is None or template is None:
            logger.debug('tool_search: could not resolve instance/template (agent_obj.pool or name missing)')
            return self._format_result([], 'No MCP discovery context available for this call.')

        func_map = getattr(template, 'function_map', None) or {}

        if not query.strip():
            # Catalog mode: read-only listing of ALL MCP tools. Do NOT touch
            # instance._discovered_mcp_tools (unlocks nothing); loaded stays [].
            entries = self._catalog(func_map)
            capped = entries[:_CATALOG_MAX]
            truncated = len(entries) > _CATALOG_MAX
            message = self._catalog_message(capped, len(entries), truncated)
            return self._format_result([], message, catalog=[e['name'] for e in capped],
                                       count=len(entries), truncated=truncated)

        # ``discovered`` is needed only by the search/unlock path; read it here so a
        # catalog request does no pointless per-call list copy.
        discovered = list(getattr(instance, '_discovered_mcp_tools', None) or [])
        docs = self._candidate_docs(func_map, discovered)
        ranked = rank_deferred_tools(query, docs, limit)

        if ranked:
            # Single-writer model: this dispatcher thread appends; the main loop (Seam A)
            # reads only. Do NOT mutate/rebind the list here — append in place (F5).
            if getattr(instance, '_discovered_mcp_tools', None) is None:
                instance._discovered_mcp_tools = []
            existing = set(instance._discovered_mcp_tools)
            for name in ranked:
                if name not in existing:
                    instance._discovered_mcp_tools.append(name)
                    existing.add(name)
            message = (f'Unlocked {len(ranked)} MCP tool(s) — available on your NEXT call: '
                       f'{', '.join(ranked)}. Call them directly; you do not need to re-search to use them.')
        else:
            message = ('No deferred MCP tools matched the query. '
                       'Try a broader term or a different tool name.')

        return self._format_result(ranked, message)

    def _format_result(self, loaded: List[str], message: str,
                       catalog: Optional[List[str]] = None, count: Optional[int] = None,
                       truncated: Optional[bool] = None) -> str:
        """Human text plus the trailing JSON envelope line (the re-derivation parse target).

        ``loaded`` (DISCOVERED_KEY) is present in EVERY envelope — populated in search mode,
        ``[]`` in catalog mode — so ``session_io`` re-derivation stays stable. When a catalog
        listing is requested, ``catalog``/``count``/``truncated`` are additionally emitted.
        ``catalog is None`` means "catalog not requested" (search mode) and omits the keys;
        an empty catalog ``[]`` is a DISTINCT shape — requested, zero tools — and still emits
        them. Gate on identity (``is not None``), never truthiness, or ``[]`` would be dropped.
        """
        inner = {DISCOVERED_KEY: loaded}
        if catalog is not None:
            inner[CATALOG_KEY] = catalog
            inner[COUNT_KEY] = count
            inner[TRUNCATED_KEY] = truncated
        envelope = json.dumps({TOOL_SEARCH_ENVELOPE_KEY: inner}, ensure_ascii=False)
        return f'{message}\n{envelope}'
