"""Module 01 orchestration and evidence lifecycle.

Declared sampling assumptions are recorded and compared, not statistically
proved. No method computes a significance test or confers a discovery grade.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import sqlite3

from .errors import AccessDenied, IntegrityError, StateError, ValidationError
from .store import (ROLES, append_event, authenticate, canonical, connect,
                    digest, load_snapshot, new_id, now, snapshot)


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f'{label} must be a nonempty string.')
    return value


def _time(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.astimezone(timezone.utc).isoformat() if parsed.tzinfo else None
    except ValueError:
        return None


def _usable(assessed):
    rows = []
    for item in assessed:
        if item['status'] in ('usable', 'flagged'):
            row = json.loads(canonical(item['record']))
            row['_quality'] = {'status': item['status'], 'reasons': item['reasons']}
            rows.append(row)
    return rows


class Module01:
    """Authenticated client. Keep the vault filesystem with its custodian."""

    def __init__(self, db_path, token):
        self.db_path = Path(db_path).resolve()
        self._token = token
        db = connect(self.db_path)
        try:
            self.role = authenticate(db, token)
        finally:
            db.close()

    @contextmanager
    def _tx(self, operation, allowed=ROLES):
        db = connect(self.db_path)
        try:
            db.execute('BEGIN IMMEDIATE')
            role = authenticate(db, self._token)
            if role not in allowed:
                raise AccessDenied('This role cannot perform the requested operation.')
            yield db
            db.commit()
        except Exception as exc:
            db.rollback()
            try:
                db.execute('BEGIN IMMEDIATE')
                append_event(db, self.role, 'operation_failed',
                             {'operation': operation, 'error_type': type(exc).__name__})
                db.commit()
            except sqlite3.Error:
                db.rollback()
            if isinstance(exc, sqlite3.IntegrityError):
                raise IntegrityError('A storage consistency constraint was violated.') from exc
            raise
        finally:
            db.close()

    def _event(self, db, action, **details):
        append_event(db, self.role, action, details)

    @staticmethod
    def _dataset(db, ref):
        _text(ref, 'dataset reference')
        row = db.execute('SELECT * FROM datasets WHERE ref=?', (ref,)).fetchone()
        if row is None:
            raise ValidationError('Unknown dataset reference.')
        return row

    @staticmethod
    def _protocol(db, protocol_id):
        row = db.execute('SELECT * FROM protocols WHERE id=?', (protocol_id,)).fetchone()
        if row is None:
            raise ValidationError('Unknown protocol.')
        return row

    @staticmethod
    def _binding(db, binding_id):
        row = db.execute('SELECT * FROM bindings WHERE id=?', (binding_id,)).fetchone()
        if row is None:
            raise ValidationError('Unknown confirmation binding.')
        if digest(row['plan']) != row['plan_digest']:
            raise IntegrityError('The frozen protocol has been modified.')
        return row

    @staticmethod
    def _check_new(db, assessed, protocol_id=None, temporal=False):
        """Reject registered content even if record IDs or file names changed."""
        units = {}
        for item in assessed:
            if db.execute('SELECT 1 FROM identities WHERE fingerprint=?',
                          (item['fingerprint'],)).fetchone():
                raise StateError('Previously registered content cannot be resealed or reingested.')
            at = _time(item['record'].get('event_time'))
            for key in item['unit_keys']:
                units.setdefault(key, []).append(at)
        for key, dates in units.items():
            old = db.execute('SELECT * FROM units WHERE unit_key=?', (key,)).fetchone()
            if old is None:
                continue
            if not temporal or old['protocol_id'] != protocol_id:
                raise StateError('Previously registered sampling units cannot be assigned to new evidence.')
            if not old['latest_time'] or any(d is None or d <= old['latest_time'] for d in dates):
                raise StateError('New temporal evidence must follow all earlier records for its units.')

    @staticmethod
    def _register(db, protocol_id, ref, assessed):
        for item in assessed:
            db.execute('INSERT OR IGNORE INTO identities VALUES (?,?,?)',
                       (item['fingerprint'], protocol_id, ref))
            at = _time(item['record'].get('event_time'))
            for key in item['unit_keys']:
                old = db.execute('SELECT latest_time FROM units WHERE unit_key=?', (key,)).fetchone()
                if old is None:
                    db.execute('INSERT INTO units VALUES (?,?,?)', (key, protocol_id, at))
                elif at and (not old['latest_time'] or at > old['latest_time']):
                    db.execute('UPDATE units SET latest_time=? WHERE unit_key=?', (at, key))

    def build(self, spec, records):
        """Build a protocol in one transaction, including private quality reports."""
        from .models import validate_spec
        from .data import assess_records, partition_records, quality_report

        with self._tx('build', ('custodian',)) as db:
            config = validate_spec(json.loads(canonical(spec)))
            if not isinstance(records, list) or not records:
                raise ValidationError('records must be a nonempty JSON list.')
            raw = json.loads(canonical(records))
            assessed = assess_records(raw, config)
            self._check_new(db, assessed)
            parts = partition_records(assessed, config)
            protocol_id = new_id('protocol')
            refs = {purpose: new_id('data') for purpose in parts}
            public = {
                'protocol_id': protocol_id, 'version': 1,
                'task': config['task'], 'observation_schema': config['schema'],
                'dependence': config['dependence'], 'split_plan': config['split'],
                'quality_rules_version': config['quality']['version'],
                'confirmation_policy': config['confirmation'],
                'identification_gaps': config.get('identification_gaps', []),
                'resources': {k: v for k, v in refs.items() if k != 'Q'},
                'readiness': {
                    'exploration': 'ready' if _usable(parts.get('E', [])) else 'needs_data',
                    'confirmation': 'requires_frozen_protocol',
                    'statistical_validity': 'requires_justified_assumptions'
                }
            }
            db.execute('INSERT INTO protocols VALUES (?,?,?,?,?)',
                       (protocol_id, 1, canonical(config), canonical(public), now()))
            raw_id = snapshot(db, protocol_id, 'raw', raw)
            for purpose, rows in parts.items():
                snap = snapshot(db, protocol_id, 'partition',
                                {'parent_snapshot_id': raw_id, 'purpose': purpose, 'rows': rows})
                report = quality_report(rows, config)
                state = 'sealed' if re.fullmatch(r'C[1-9]\d*', purpose) else 'open'
                db.execute('INSERT INTO datasets VALUES (?,?,?,?,?,?,NULL)',
                           (refs[purpose], protocol_id, purpose, state, snap, canonical(report)))
                self._register(db, protocol_id, refs[purpose], rows)
            self._event(db, 'build', protocol_id=protocol_id)
            return public

    def describe(self, protocol_id):
        with self._tx('describe') as db:
            row = self._protocol(db, protocol_id)
            public = json.loads(row['public'])
            resources = db.execute('SELECT purpose,ref FROM datasets WHERE protocol_id=?',
                                   (protocol_id,)).fetchall()
            public['resources'] = {r['purpose']: r['ref'] for r in resources if r['purpose'] != 'Q'}
            self._event(db, 'describe', protocol_id=protocol_id)
            return public

    def read_dataset(self, ref):
        with self._tx('read_dataset', ('explorer', 'custodian')) as db:
            row = self._dataset(db, ref)
            if row['purpose'] not in ('E', 'V') and not row['purpose'].startswith('H_'):
                raise AccessDenied('This resource is not an exploration/development view.')
            if row['state'] not in ('open', 'historical'):
                raise AccessDenied('This evidence is not open for exploration.')
            data = load_snapshot(db, row['snapshot_id'])
            self._event(db, 'read', dataset_ref=ref)
            return _usable(data['rows'])

    def quality(self, ref):
        with self._tx('quality', ('explorer', 'custodian', 'auditor')) as db:
            row = self._dataset(db, ref)
            if self.role == 'explorer' and (row['purpose'] not in ('E', 'V') and
                                          not row['purpose'].startswith('H_')):
                raise AccessDenied('Confirmation quality reports are restricted.')
            # Also validate the underlying immutable content before reporting it.
            load_snapshot(db, row['snapshot_id'])
            self._event(db, 'quality', dataset_ref=ref, restricted=row['purpose'].startswith('C'))
            return json.loads(row['quality'])

    def _validate_plan(self, db, dataset, plan):
        config = json.loads(self._protocol(db, dataset['protocol_id'])['spec'])
        if not isinstance(plan, dict):
            raise ValidationError('The frozen plan must be an object.')
        if plan.get('protocol_id') != dataset['protocol_id']:
            raise ValidationError('The frozen plan belongs to another protocol.')
        round_index = plan.get('round_index')
        if isinstance(round_index, bool) or not isinstance(round_index, int) or not 1 <= round_index <= 1022:
            raise ValidationError('round_index must be an integer from 1 to 1022.')
        hypotheses = plan.get('hypotheses')
        if not isinstance(hypotheses, list) or not hypotheses:
            raise ValidationError('A finite nonempty hypothesis list is required.')
        hids = set()
        for h in hypotheses:
            if not isinstance(h, dict):
                raise ValidationError('Each hypothesis must be an object.')
            for field in ('id', 'version', 'statement', 'prediction', 'scope', 'model'):
                _text(h.get(field), 'hypothesis.' + field)
            if h['id'] in hids:
                raise ValidationError('Hypothesis identifiers must be unique.')
            hids.add(h['id'])
            if not isinstance(h.get('representation'), dict) or not isinstance(h.get('parameters'), dict):
                raise ValidationError('Hypothesis representation and parameters must be frozen objects.')
        preprocessing = plan.get('preprocessing')
        if not isinstance(preprocessing, dict) or not isinstance(preprocessing.get('steps'), list):
            raise ValidationError('Frozen preprocessing steps are required (empty list is allowed).')
        fit_refs = preprocessing.get('fit_dataset_refs')
        if not isinstance(fit_refs, list) or not fit_refs:
            raise ValidationError('Declare the exploration references used for fitting.')
        for ref in fit_refs:
            fit_data = self._dataset(db, ref)
            if fit_data['protocol_id'] != dataset['protocol_id']:
                raise ValidationError('Fit references must belong to this protocol.')
            if fit_data['purpose'] != 'E' and not fit_data['purpose'].startswith('H_'):
                raise ValidationError('Fitting on development or confirmation data is not permitted.')
        for field in ('primary_metric', 'sampling_plan', 'stopping_rule', 'inference_unit',
                      'eligibility', 'quality_rules_version'):
            _text(plan.get(field), field)
        for field in ('sampling_plan', 'stopping_rule'):
            if plan[field] != config['confirmation'][field]:
                raise ValidationError(f'{field} does not match the data protocol.')
        if plan['inference_unit'] != config['dependence']['inference_unit']:
            raise ValidationError('The inference unit does not match the data protocol.')
        if plan['eligibility'] != config['task']['eligibility']:
            raise ValidationError('The target eligibility does not match the data protocol.')
        if plan['quality_rules_version'] != config['quality']['version']:
            raise ValidationError('Quality rules changed after protocol registration.')
        effect = plan.get('effect_threshold')
        if isinstance(effect, bool) or not isinstance(effect, (int, float)) or not math.isfinite(effect):
            raise ValidationError('A finite effect_threshold is required.')
        family = plan.get('test_family')
        if not isinstance(family, list) or not family:
            raise ValidationError('A complete finite test family must be frozen.')
        tids, covered = set(), set()
        for test in family:
            if not isinstance(test, dict):
                raise ValidationError('Each declared test must be an object.')
            for field in ('id', 'hypothesis_id', 'null', 'alternative', 'method'):
                _text(test.get(field), 'test.' + field)
            if test['id'] in tids or test['hypothesis_id'] not in hids:
                raise ValidationError('Test identity is duplicated or references an unknown hypothesis.')
            tids.add(test['id'])
            covered.add(test['hypothesis_id'])
        if covered != hids:
            raise ValidationError('Every claimed hypothesis needs a declared test.')
        assumptions = plan.get('assumptions')
        if not isinstance(assumptions, list) or not assumptions:
            raise ValidationError('Explicit assumptions with justifications are required.')
        for assumption in assumptions:
            if not isinstance(assumption, dict):
                raise ValidationError('Assumptions must be named and justified.')
            _text(assumption.get('name'), 'assumption.name')
            _text(assumption.get('justification'), 'assumption.justification')
        alpha = math.ldexp(config['confirmation']['total_alpha'], -round_index)
        if alpha <= 0:
            raise ValidationError('The requested error budget underflows; choose a supported round.')
        return round_index, alpha

    def bind_confirmation(self, ref, plan):
        with self._tx('bind_confirmation', ('confirmer',)) as db:
            data = self._dataset(db, ref)
            if data['state'] != 'sealed' or not data['purpose'].startswith('C'):
                raise StateError('Only unused sealed confirmation resources can be bound.')
            frozen = json.loads(canonical(plan))
            round_index, alpha = self._validate_plan(db, data, frozen)
            if db.execute('SELECT 1 FROM bindings WHERE protocol_id=? AND round_index=?',
                          (data['protocol_id'], round_index)).fetchone():
                raise StateError('This round already has a frozen confirmation batch.')
            if not _usable(load_snapshot(db, data['snapshot_id'])['rows']):
                raise StateError('No eligible confirmation observations are available.')
            binding_id = new_id('binding')
            body = canonical(frozen)
            plan_digest = digest(body)
            db.execute('INSERT INTO bindings VALUES (?,?,?,?,?,?,?,0)',
                       (binding_id, data['protocol_id'], ref, body, plan_digest, alpha, round_index))
            db.execute("UPDATE datasets SET state='bound_to_frozen_protocol',binding_id=? WHERE ref=?",
                       (binding_id, ref))
            self._event(db, 'freeze', binding_id=binding_id, dataset_ref=ref,
                        plan_digest=plan_digest, round_index=round_index, alpha=alpha)
            return {'binding_id': binding_id, 'plan_digest': plan_digest,
                    'alpha': alpha, 'state': 'bound_to_frozen_protocol'}

    def consume_confirmation(self, binding_id, replay=False):
        with self._tx('consume_confirmation', ('confirmer',)) as db:
            if not isinstance(replay, bool):
                raise ValidationError('replay must be a boolean.')
            binding = self._binding(db, binding_id)
            data = self._dataset(db, binding['dataset_ref'])
            if data['binding_id'] != binding_id:
                raise IntegrityError('Confirmation binding and dataset disagree.')
            if data['state'] == 'bound_to_frozen_protocol' and not replay:
                db.execute("UPDATE datasets SET state='used' WHERE ref=?", (data['ref'],))
                self._event(db, 'consume', binding_id=binding_id, dataset_ref=data['ref'])
            elif data['state'] in ('used', 'historical') and replay:
                self._event(db, 'replay', binding_id=binding_id, dataset_ref=data['ref'], new_evidence=False)
            else:
                raise StateError('Confirmation was already consumed or is not eligible; explicit replay is required.')
            # Exit the transaction before loading/returning ANY confirmation content.
            snapshot_id = data['snapshot_id']
        db = connect(self.db_path)
        try:
            return _usable(load_snapshot(db, snapshot_id)['rows'])
        finally:
            db.close()

    def record_evaluation(self, binding_id, result):
        with self._tx('record_evaluation', ('confirmer',)) as db:
            binding = self._binding(db, binding_id)
            data = self._dataset(db, binding['dataset_ref'])
            if data['state'] != 'used':
                raise StateError('Results can only be recorded for consumed evidence.')
            value = json.loads(canonical(result))
            if not isinstance(value, dict) or value.get('status') not in ('supported','refuted','inconclusive','failed'):
                raise ValidationError('Result status is invalid.')
            if not isinstance(value.get('metrics'), dict):
                raise ValidationError('Result metrics must be an object.')
            _text(value.get('code_version'), 'code_version')
            if not isinstance(value.get('notes'), str):
                raise ValidationError('Result notes must be a string.')
            if binding['released']:
                raise StateError('Released results cannot be changed.')
            previous = db.execute("SELECT 1 FROM evaluations WHERE binding_id=? AND status!='failed'",
                                  (binding_id,)).fetchone()
            if previous:
                raise StateError('The final result is immutable; failed attempts remain in its history.')
            value['recorded_by'] = 'external_confirmation_executor'
            value['binding_id'] = binding_id
            db.execute('INSERT INTO evaluations(binding_id,payload,status,created_at) VALUES (?,?,?,?)',
                       (binding_id, canonical(value), value['status'], now()))
            self._event(db, 'evaluate', binding_id=binding_id, status=value['status'])
            return value

    @staticmethod
    def _evaluation_payload(db, binding_id):
        rows = db.execute('SELECT payload FROM evaluations WHERE binding_id=? ORDER BY seq',
                          (binding_id,)).fetchall()
        return {'binding_id': binding_id, 'evaluations': [json.loads(r['payload']) for r in rows]}

    def release_results(self, binding_id):
        with self._tx('release_results', ('confirmer',)) as db:
            binding = self._binding(db, binding_id)
            payload = self._evaluation_payload(db, binding_id)
            if not payload['evaluations']:
                raise StateError('There are no recorded evaluation results to release.')
            if self._dataset(db, binding['dataset_ref'])['state'] not in ('used', 'historical'):
                raise StateError('The batch is not in a releasable state.')
            db.execute('UPDATE bindings SET released=1 WHERE id=?', (binding_id,))
            self._event(db, 'release', binding_id=binding_id)
            return payload

    def results(self, binding_id):
        with self._tx('results') as db:
            binding = self._binding(db, binding_id)
            if self.role == 'explorer' and not binding['released']:
                raise AccessDenied('Evaluation results have not been released.')
            self._event(db, 'read_results', binding_id=binding_id)
            return self._evaluation_payload(db, binding_id)

    def archive_confirmation(self, binding_id):
        from .data import quality_report

        with self._tx('archive_confirmation', ('custodian',)) as db:
            binding = self._binding(db, binding_id)
            data = self._dataset(db, binding['dataset_ref'])
            purpose = 'H_' + binding_id
            old = db.execute('SELECT ref FROM datasets WHERE protocol_id=? AND purpose=?',
                             (data['protocol_id'], purpose)).fetchone()
            if old:
                return {'historical_ref': old['ref']}
            if data['state'] != 'used' or not binding['released']:
                raise StateError('Only consumed batches with released results may enter exploration history.')
            ref = new_id('data')
            # A view of the original snapshot preserves identity; it is not a new sample.
            db.execute('INSERT INTO datasets VALUES (?,?,?,?,?,?,?)',
                       (ref, data['protocol_id'], purpose, 'historical',
                        data['snapshot_id'], data['quality'], binding_id))
            db.execute("UPDATE datasets SET state='historical' WHERE ref=?", (data['ref'],))
            self._event(db, 'reclassify', binding_id=binding_id, source_ref=data['ref'], historical_ref=ref)
            return {'historical_ref': ref}

    def mark_compromised(self, ref, reason):
        with self._tx('mark_compromised', ('custodian',)) as db:
            _text(reason, 'reason')
            data = self._dataset(db, ref)
            if data['state'] not in ('sealed', 'bound_to_frozen_protocol'):
                raise StateError('Only unused confirmation batches can be marked compromised.')
            db.execute("UPDATE datasets SET state='compromised' WHERE ref=?", (ref,))
            self._event(db, 'compromise', dataset_ref=ref, reason=reason)
            return {'ref': ref, 'state': 'compromised'}

    def inspect_confirmation(self, ref, reason):
        """Custodian inspection retires confirmation eligibility BEFORE read."""
        self.mark_compromised(ref, reason)
        with self._tx('inspect_confirmation', ('custodian',)) as db:
            data = self._dataset(db, ref)
            self._event(db, 'inspect', dataset_ref=ref, reason=reason)
            return load_snapshot(db, data['snapshot_id'])['rows']

    def add_confirmation(self, protocol_id, records, purpose):
        from .data import assess_records, quality_report

        with self._tx('add_confirmation', ('custodian',)) as db:
            if not isinstance(purpose, str) or not re.fullmatch(r'C[1-9]\d*', purpose):
                raise ValidationError('New confirmation purpose must be C1, C2, ...')
            protocol = self._protocol(db, protocol_id)
            if db.execute('SELECT 1 FROM datasets WHERE protocol_id=? AND purpose=?', (protocol_id,purpose)).fetchone():
                raise StateError('This confirmation purpose already exists.')
            if not isinstance(records, list) or not records:
                raise ValidationError('New evidence requires a nonempty record list.')
            config = json.loads(protocol['spec'])
            raw = json.loads(canonical(records))
            assessed = assess_records(raw, config)
            self._check_new(db, assessed, protocol_id,
                            temporal=config['split']['strategy'] == 'temporal')
            ref = new_id('data')
            raw_id = snapshot(db, protocol_id, 'raw_addition', raw)
            snap = snapshot(db, protocol_id, 'partition',
                            {'parent_snapshot_id':raw_id, 'purpose':purpose, 'rows':assessed})
            db.execute('INSERT INTO datasets VALUES (?,?,?,?,?,?,NULL)',
                       (ref,protocol_id,purpose,'sealed',snap,canonical(quality_report(assessed,config))))
            self._register(db, protocol_id, ref, assessed)
            self._event(db, 'add_confirmation', protocol_id=protocol_id, dataset_ref=ref)
            return {'ref':ref}

    def ledger(self):
        with self._tx('ledger', ('custodian','auditor')) as db:
            rows = db.execute('SELECT * FROM ledger ORDER BY seq').fetchall()
            output = [dict(r) for r in rows]
            for row in output:
                row['details'] = json.loads(row['details'])
            self._event(db, 'audit')
            return output

    def verify_integrity(self):
        with self._tx('verify_integrity', ('custodian','auditor')) as db:
            previous, expected = '0' * 64, 1
            for row in db.execute('SELECT * FROM ledger ORDER BY seq'):
                try:
                    event = dict(row)
                    event_hash = event.pop('event_hash')
                    event['details'] = json.loads(event['details'])
                except (TypeError, ValueError) as exc:
                    raise IntegrityError('Ledger encoding is invalid.') from exc
                if row['seq'] != expected or row['prev_hash'] != previous or digest(canonical(event)) != event_hash:
                    raise IntegrityError('Ledger chain verification failed.')
                previous, expected = event_hash, expected + 1
            count = 0
            for row in db.execute('SELECT id FROM snapshots'):
                load_snapshot(db, row['id'])
                count += 1
            bindings = db.execute('SELECT id FROM bindings').fetchall()
            for row in bindings:
                self._binding(db, row['id'])
            fk = db.execute('PRAGMA foreign_key_check').fetchone()
            if fk is not None:
                raise IntegrityError('Stored references are inconsistent.')
            self._event(db, 'verify', snapshots=count)
            return {'status':'ok', 'snapshots':count, 'ledger_events':expected-1,
                    'frozen_plans':len(bindings), 'scope':'content_and_record_consistency_not_truth'}
