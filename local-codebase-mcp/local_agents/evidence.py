"""Convert proposed citations into schema-valid, source-checked evidence.

Validation establishes bytes/line identity, not semantic truth of a model claim.
Snapshot internal invalid diagnostics never pass directly into persisted_report.
"""
from copy import deepcopy
import os

from errors import McpError
from local_agents.contracts import validation_errors
from local_agents.snapshot import read_snapshot_evidence
from local_agents.store import StoreError
from security import is_sensitive, resolve_path


def verify_evidence(project, snapshot, candidates):
    """Return (final Evidence objects, safe issue records); reject scope escapes."""
    if not isinstance(candidates, (list, tuple)):
        raise StoreError('INVALID_ARGUMENT')
    final, issues, identifiers = [], [], set()
    root = resolve_path(project, '.')
    policy = snapshot['coverage']['scope']
    for proposed in candidates:
        if validation_errors('evidence', proposed) or proposed['evidence_id'] in identifiers:
            raise StoreError('INVALID_ARGUMENT')
        identifiers.add(proposed['evidence_id'])
        try:
            relative = resolve_path(project, proposed['path']).relative_to(root).as_posix()
        except (McpError, ValueError, OSError):
            raise StoreError('POLICY_DENIED') from None
        if (relative not in snapshot['files'] or is_sensitive(relative, policy['sensitive_patterns']) or
                any(os.path.normcase(part) in policy['excluded_dirs'] for part in relative.split('/'))):
            raise StoreError('POLICY_DENIED')
        item = deepcopy(proposed)
        item.update(path=relative, snapshot_id=snapshot['snapshot_id'],
                    content_sha256=snapshot['files'][relative]['content_sha256'], excerpt='', validation='invalid')
        reason = None
        if proposed['snapshot_id'] != item['snapshot_id']:
            reason = 'snapshot_mismatch'
        elif proposed['content_sha256'] != item['content_sha256']:
            reason = 'content_hash_mismatch'
        elif proposed['line_start'] is None or proposed['line_end'] is None:
            item['validation'] = 'unverified'
            reason = 'symbol_resolution_not_verified'
        else:
            try:
                source = read_snapshot_evidence(project, snapshot, relative,
                    line_start=proposed['line_start'], line_end=proposed['line_end'])
            except (ValueError, McpError, OSError):
                source = None
            if source is None or source['validation'] != 'verified':
                reason = 'source_changed_or_range_invalid'
            elif source['excerpt'] != proposed['excerpt']:
                reason = 'excerpt_mismatch'
            elif len(source['excerpt']) > 4000:
                reason = 'excerpt_budget_exceeded'
            else:
                item['excerpt'] = source['excerpt']
                item['validation'] = 'verified' if proposed['symbol'] is None else 'unverified'
                if proposed['symbol'] is not None:
                    reason = 'symbol_resolution_not_verified'
        if validation_errors('evidence', item):
            raise StoreError('INVALID_ARGUMENT')
        final.append(item)
        if reason:
            issues.append({'evidence_id': item['evidence_id'], 'reason': reason})
    return final, issues
