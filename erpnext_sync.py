import importlib
import importlib.util
import requests
import datetime
import json
import os
import sys
import time
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from logging.handlers import RotatingFileHandler
from pickledb import PickleDB
from zk import ZK, const

def _load_runtime_config():
    if getattr(sys, 'frozen', False):
        from config_store import CONFIG_PATH
        config_path = str(CONFIG_PATH)
        if not os.path.exists(config_path):
            raise RuntimeError('Save ERPNext configuration in PulseBridge before synchronization can run.')
        spec = importlib.util.spec_from_file_location('pulsebridge_runtime_config', config_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    return importlib.import_module('local_config')

config = _load_runtime_config()

EMPLOYEE_NOT_FOUND_ERROR_MESSAGE = "No Employee found for the given employee field value"
EMPLOYEE_INACTIVE_ERROR_MESSAGE = "Transactions cannot be created for an Inactive Employee"
DUPLICATE_EMPLOYEE_CHECKIN_ERROR_MESSAGE = "This employee already has a log with the same timestamp"
allowlisted_errors = [EMPLOYEE_NOT_FOUND_ERROR_MESSAGE, EMPLOYEE_INACTIVE_ERROR_MESSAGE, DUPLICATE_EMPLOYEE_CHECKIN_ERROR_MESSAGE]

if hasattr(config, 'allowed_exceptions'):
    allowlisted_errors_temp = []
    for error_number in config.allowed_exceptions:
        allowlisted_errors_temp.append(allowlisted_errors[error_number - 1])
    allowlisted_errors = allowlisted_errors_temp

device_punch_values_IN = getattr(config, 'device_punch_values_IN', [0, 4])
device_punch_values_OUT = getattr(config, 'device_punch_values_OUT', [1, 5])

ERPNEXT_VERSION = getattr(config, 'ERPNEXT_VERSION', 14)
SHIFT_LOGIC = getattr(config, 'SHIFT_LOGIC', {})
DEVICE_DEFAULT_SHIFT = getattr(config, 'DEVICE_DEFAULT_SHIFT', {})
SHIFT_BOUNDARIES = getattr(config, 'SHIFT_BOUNDARIES', {})
AUTO_SHIFT_DETECTION_ENABLED = getattr(config, 'AUTO_SHIFT_DETECTION_ENABLED', False)
AUTO_SHIFT_EXCLUDED_IPS = getattr(config, 'AUTO_SHIFT_EXCLUDED_IPS', [])
AUTO_SHIFT_ALLOWED_BY_DEVICE = getattr(config, 'AUTO_SHIFT_ALLOWED_BY_DEVICE', {})
AUTO_SHIFT_MAX_DISTANCE_MINUTES = getattr(config, 'AUTO_SHIFT_MAX_DISTANCE_MINUTES', 240)
AUTO_CREATE_SHIFT_ASSIGNMENT = getattr(config, 'AUTO_CREATE_SHIFT_ASSIGNMENT', True)
AUTO_CREATED_SHIFT_ASSIGNMENT_PREFIX = getattr(config, 'AUTO_CREATED_SHIFT_ASSIGNMENT_PREFIX', 'AUTO-SYNC')
SYNC_DEVICE_TIME_WITH_PC = getattr(config, 'SYNC_DEVICE_TIME_WITH_PC', True)
AUTO_CORRECT_SINGLE_PUNCHES = getattr(config, 'AUTO_CORRECT_SINGLE_PUNCHES', True)
SINGLE_PUNCH_CORRECTION_BY_DEVICE = getattr(config, 'SINGLE_PUNCH_CORRECTION_BY_DEVICE', {})
SINGLE_PUNCH_GRACE_BY_SHIFT = getattr(config, 'SINGLE_PUNCH_GRACE_BY_SHIFT', {})
IMPORT_START_DATE = getattr(config, 'IMPORT_START_DATE', None)
IMPORT_END_DATE = getattr(config, 'IMPORT_END_DATE', None)
RANGE_SYNC = os.getenv('PULSEBRIDGE_RANGE_SYNC') == '1'
try:
    REQUESTED_DEVICE_IDS = {
        str(value) for value in json.loads(os.getenv('PULSEBRIDGE_DEVICE_IDS', '[]')) if str(value)
    }
except (TypeError, ValueError, json.JSONDecodeError):
    REQUESTED_DEVICE_IDS = set()
TEMP_SHIFT_ASSIGNMENT_CACHE = set()
PENDING_PUNCH_LOCK = threading.Lock()
STATUS_LOCK = threading.RLock()
EMPLOYEE_LOOKUP_CACHE = {}
SHIFT_ASSIGNMENT_CACHE = {}
ERP_CACHE_LOCK = threading.RLock()
HTTP_LOCAL = threading.local()

def _http_session():
    session = getattr(HTTP_LOCAL, 'session', None)
    if session is None:
        session = requests.Session()
        session.headers.update(_erp_headers())
        HTTP_LOCAL.session = session
    return session

def _erp_headers():
    return {
        'Authorization': "token " + config.ERPNEXT_API_KEY + ":" + config.ERPNEXT_API_SECRET,
        'Accept': 'application/json'
    }

def _erp_get(resource_path, params=None):
    url = f"{config.ERPNEXT_URL}{resource_path}"
    response = _http_session().get(url, params=params, timeout=30)
    response.raise_for_status()
    return response.json().get('data', [])

def _parse_hhmm(value):
    hour, minute = map(int, value.split(':'))
    return hour * 60 + minute

def _minutes_of_day(dt):
    return dt.hour * 60 + dt.minute

def _minutes_distance(a, b):
    diff = abs(a - b)
    return min(diff, 1440 - diff)

def _shift_occurrence(shift_name, punch_dt):
    """Return assignment date and exact start/end datetimes for a punch's shift occurrence."""
    bounds = SHIFT_BOUNDARIES.get(shift_name, {})
    if not bounds.get('start') or not bounds.get('end'):
        return punch_dt.date(), None, None

    start_minutes = _parse_hhmm(bounds['start'])
    end_minutes = _parse_hhmm(bounds['end'])
    assignment_date = punch_dt.date()
    if start_minutes > end_minutes and _minutes_of_day(punch_dt) <= end_minutes:
        assignment_date -= datetime.timedelta(days=1)

    start_time = datetime.time(start_minutes // 60, start_minutes % 60)
    end_time = datetime.time(end_minutes // 60, end_minutes % 60)
    start_dt = datetime.datetime.combine(assignment_date, start_time)
    end_date = assignment_date + datetime.timedelta(days=1) if start_minutes > end_minutes else assignment_date
    end_dt = datetime.datetime.combine(end_date, end_time)
    return assignment_date, start_dt, end_dt

def _shift_datetimes_for_assignment_date(shift_name, assignment_date):
    """Return exact shift boundaries when the Shift Assignment date is already known."""
    bounds = SHIFT_BOUNDARIES.get(shift_name, {})
    if not bounds.get('start') or not bounds.get('end'):
        return None, None
    if isinstance(assignment_date, datetime.datetime):
        assignment_date = assignment_date.date()
    start_minutes = _parse_hhmm(bounds['start'])
    end_minutes = _parse_hhmm(bounds['end'])
    start_dt = datetime.datetime.combine(
        assignment_date, datetime.time(start_minutes // 60, start_minutes % 60)
    )
    end_date = assignment_date + datetime.timedelta(days=1) if start_minutes > end_minutes else assignment_date
    end_dt = datetime.datetime.combine(
        end_date, datetime.time(end_minutes // 60, end_minutes % 60)
    )
    return start_dt, end_dt

def _in_time_window(dt, start_str, end_str):
    value = _minutes_of_day(dt)
    start = _parse_hhmm(start_str)
    end = _parse_hhmm(end_str)
    if start <= end:
        return start <= value <= end
    return value >= start or value <= end

def _matches_any_window(dt, windows):
    for start_str, end_str in windows:
        if _in_time_window(dt, start_str, end_str):
            return True
    return False

def _get_employee_by_attendance_device_id(attendance_device_id):
    cache_key = str(attendance_device_id)
    with ERP_CACHE_LOCK:
        if cache_key in EMPLOYEE_LOOKUP_CACHE:
            return EMPLOYEE_LOOKUP_CACHE[cache_key]
    params = {
        'fields': json.dumps(['name', 'default_shift', 'attendance_device_id', 'company']),
        'filters': json.dumps([['attendance_device_id', '=', str(attendance_device_id)]]),
        'limit_page_length': 1
    }
    employees = _erp_get('/api/resource/Employee', params=params)
    employee = employees[0] if employees else None
    with ERP_CACHE_LOCK:
        EMPLOYEE_LOOKUP_CACHE[cache_key] = employee
    return employee

def _get_active_shift_assignment(employee_name, punch_date):
    with ERP_CACHE_LOCK:
        assignments = SHIFT_ASSIGNMENT_CACHE.get(employee_name)
    if assignments is None:
        params = {
            'fields': json.dumps(['name', 'shift_type', 'start_date', 'end_date', 'status']),
            'filters': json.dumps([
                ['employee', '=', employee_name],
                ['docstatus', '=', 1],
                ['status', '=', 'Active']
            ]),
            'order_by': 'start_date desc',
            'limit_page_length': 1000
        }
        assignments = _erp_get('/api/resource/Shift Assignment', params=params)
        with ERP_CACHE_LOCK:
            SHIFT_ASSIGNMENT_CACHE[employee_name] = assignments
    punch_date_str = punch_date.strftime('%Y-%m-%d')
    for assignment in assignments:
        start_date = assignment.get('start_date')
        end_date = assignment.get('end_date')
        if (not start_date or start_date <= punch_date_str) and (not end_date or end_date >= punch_date_str):
            return assignment.get('shift_type')
    return None

def _ensure_temporary_shift_assignment(employee, shift_name, punch_date):
    if not AUTO_CREATE_SHIFT_ASSIGNMENT or not employee or not shift_name:
        return False

    date_str = punch_date.strftime('%Y-%m-%d')
    cache_key = f"{employee['name']}::{shift_name}::{date_str}"
    if cache_key in TEMP_SHIFT_ASSIGNMENT_CACHE:
        return True
    with ERP_CACHE_LOCK:
        cached_assignments = SHIFT_ASSIGNMENT_CACHE.get(employee['name'], [])
        for assignment in cached_assignments:
            start_date = assignment.get('start_date')
            end_date = assignment.get('end_date')
            if (assignment.get('shift_type') == shift_name
                    and (not start_date or start_date <= date_str)
                    and (not end_date or end_date >= date_str)):
                TEMP_SHIFT_ASSIGNMENT_CACHE.add(cache_key)
                return True

    try:
        existing = _erp_get('/api/resource/Shift Assignment', params={
            'fields': json.dumps(['name', 'shift_type', 'start_date', 'end_date', 'status', 'docstatus']),
            'filters': json.dumps([
                ['employee', '=', employee['name']],
                ['docstatus', '=', 1],
                ['status', '=', 'Active'],
                ['start_date', '<=', date_str],
                ['end_date', '>=', date_str]
            ]),
            'limit_page_length': 20
        })
        if existing:
            for assignment in existing:
                if assignment.get('shift_type') == shift_name:
                    TEMP_SHIFT_ASSIGNMENT_CACHE.add(cache_key)
                    return True

        payload = {
            'employee': employee['name'],
            'employee_name': employee.get('name'),
            'company': employee.get('company'),
            'shift_type': shift_name,
            'start_date': date_str,
            'end_date': date_str,
            'status': 'Active'
        }

        create_res = _http_session().post(
            f"{config.ERPNEXT_URL}/api/resource/Shift Assignment",
            json=payload,
            timeout=30
        )
        create_res.raise_for_status()
        created_doc = create_res.json().get('data', {})

        submit_res = _http_session().post(
            f"{config.ERPNEXT_URL}/api/method/frappe.client.submit",
            json={'doc': created_doc},
            timeout=30
        )
        if submit_res.status_code == 200:
            TEMP_SHIFT_ASSIGNMENT_CACHE.add(cache_key)
            with ERP_CACHE_LOCK:
                if employee['name'] in SHIFT_ASSIGNMENT_CACHE:
                    SHIFT_ASSIGNMENT_CACHE[employee['name']].insert(0, {
                        'name': created_doc.get('name'),
                        'shift_type': shift_name,
                        'start_date': date_str,
                        'end_date': date_str,
                        'status': 'Active',
                    })
            info_logger.info("\t".join([
                "Auto-created Shift Assignment",
                employee['name'],
                shift_name,
                date_str
            ]))
            return True
        else:
            error_logger.error("\t".join([
                "Failed to submit auto Shift Assignment",
                employee['name'],
                shift_name,
                date_str,
                _safe_get_error_str(submit_res)
            ]))
            return False
    except Exception:
        error_logger.exception(
            f"Exception creating temporary Shift Assignment for employee={employee.get('name')} shift={shift_name} date={date_str}"
        )
        return False

def _raw_punch_hint(device_attendance_log):
    punch = device_attendance_log.get('punch')
    if punch in device_punch_values_IN:
        return 'IN'
    if punch in device_punch_values_OUT:
        return 'OUT'
    return None

def _match_shift_candidate(shift_name, punch_dt):
    rules = SHIFT_LOGIC.get(shift_name, {})
    bounds = SHIFT_BOUNDARIES.get(shift_name, {})
    punch_minutes = _minutes_of_day(punch_dt)
    matches = []

    start_time = bounds.get('start')
    if start_time and _matches_any_window(punch_dt, rules.get('in', [])):
        matches.append({
            'shift_name': shift_name,
            'match_type': 'IN',
            'distance': _minutes_distance(punch_minutes, _parse_hhmm(start_time))
        })

    end_time = bounds.get('end')
    if end_time and _matches_any_window(punch_dt, rules.get('out', [])):
        matches.append({
            'shift_name': shift_name,
            'match_type': 'OUT',
            'distance': _minutes_distance(punch_minutes, _parse_hhmm(end_time))
        })

    return matches

def _infer_shift_from_time(device, punch_dt, raw_hint=None):
    if not AUTO_SHIFT_DETECTION_ENABLED:
        return None

    device_ip = device.get('ip')
    if device_ip in AUTO_SHIFT_EXCLUDED_IPS:
        return None

    allowed_shifts = AUTO_SHIFT_ALLOWED_BY_DEVICE.get(device_ip, [])
    if not allowed_shifts:
        return None

    candidates = []
    for shift_name in allowed_shifts:
        candidates.extend(_match_shift_candidate(shift_name, punch_dt))

    if not candidates:
        return None

    filtered = candidates
    if raw_hint:
        matching_hint = [c for c in candidates if c['match_type'] == raw_hint]
        if matching_hint:
            filtered = matching_hint

    filtered = [c for c in filtered if c['distance'] <= AUTO_SHIFT_MAX_DISTANCE_MINUTES]
    if not filtered:
        return None

    filtered.sort(key=lambda x: x['distance'])
    return filtered[0]['shift_name']

def _shift_matches_punch(shift_name, punch_dt, raw_hint=None):
    if not shift_name:
        return False

    rules = SHIFT_LOGIC.get(shift_name, {})
    in_match = _matches_any_window(punch_dt, rules.get('in', []))
    out_match = _matches_any_window(punch_dt, rules.get('out', []))

    if raw_hint == 'IN':
        return in_match
    if raw_hint == 'OUT':
        return out_match

    return in_match or out_match

def _resolve_shift_for_punch(employee_field_value, punch_dt, device, raw_hint=None):
    employee = None
    active_shift = None
    default_shift = None

    try:
        employee = _get_employee_by_attendance_device_id(employee_field_value)
        if employee:
            active_shift = _get_active_shift_assignment(employee['name'], punch_dt.date())
            default_shift = employee.get('default_shift')
    except Exception:
        error_logger.exception(
            f"Shift lookup failed for attendance_device_id={employee_field_value}, device={device.get('device_id')}"
        )

    # ERPNext's roster is authoritative. Some ZKTeco terminals leave every
    # punch marked as IN, so a raw device hint must not override an existing
    # shift when the punch time fits that rostered shift.
    if _shift_matches_punch(active_shift, punch_dt, raw_hint=None):
        return active_shift

    inferred_shift = _infer_shift_from_time(device, punch_dt, raw_hint=raw_hint)
    if inferred_shift:
        if employee:
            assignment_date, _, _ = _shift_occurrence(inferred_shift, punch_dt)
            if not _ensure_temporary_shift_assignment(employee, inferred_shift, assignment_date):
                raise RuntimeError(
                    f'Punch held because Shift Assignment could not be confirmed for employee={employee["name"]}, '
                    f'shift={inferred_shift}, date={assignment_date}'
                )
        return inferred_shift

    if _shift_matches_punch(default_shift, punch_dt, raw_hint=raw_hint):
        return default_shift

    device_shift = DEVICE_DEFAULT_SHIFT.get(device.get('ip')) or DEVICE_DEFAULT_SHIFT.get(device.get('device_id'))
    if device_shift and employee:
        assignment_date, _, _ = _shift_occurrence(device_shift, punch_dt)
        if not _ensure_temporary_shift_assignment(employee, device_shift, assignment_date):
            raise RuntimeError(
                f'Punch held because Shift Assignment could not be confirmed for employee={employee["name"]}, '
                f'shift={device_shift}, date={assignment_date}'
            )

    return device_shift

def resolve_punch_context(device, device_attendance_log):
    configured = device.get('punch_direction')
    raw_hint = _raw_punch_hint(device_attendance_log)
    shift_name = _resolve_shift_for_punch(
        device_attendance_log['user_id'],
        device_attendance_log['timestamp'],
        device,
        raw_hint=raw_hint
    )

    direction = configured if configured in ('IN', 'OUT') else None
    if shift_name and not direction:
        rules = SHIFT_LOGIC.get(shift_name, {})
        in_match = _matches_any_window(device_attendance_log['timestamp'], rules.get('in', []))
        out_match = _matches_any_window(device_attendance_log['timestamp'], rules.get('out', []))
        if in_match and not out_match:
            direction = 'IN'
        elif out_match and not in_match:
            direction = 'OUT'
    if not direction:
        direction = raw_hint

    assignment_date, shift_start, shift_end = _shift_occurrence(
        shift_name, device_attendance_log['timestamp']
    ) if shift_name else (device_attendance_log['timestamp'].date(), None, None)
    return {
        'direction': direction,
        'shift_name': shift_name,
        'assignment_date': assignment_date,
        'shift_start': shift_start,
        'shift_end': shift_end,
    }

def determine_punch_direction(device, device_attendance_log):
    return resolve_punch_context(device, device_attendance_log)['direction']

def _pending_punch_file():
    return os.path.join(config.LOGS_DIRECTORY, 'pending_single_punches.json')

def _load_pending_punches():
    path = _pending_punch_file()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            data = json.load(handle)
            return data if isinstance(data, dict) else {}
    except Exception:
        error_logger.exception('Unable to read pending single-punch state.')
        return {}

def _save_pending_punches(data):
    path = _pending_punch_file()
    temp_path = path + '.tmp'
    with open(temp_path, 'w', encoding='utf-8') as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
    os.replace(temp_path, path)

def _pending_key(employee_field_value, shift_name, assignment_date):
    return f'{employee_field_value}::{shift_name}::{assignment_date.isoformat()}'

def _device_correction_enabled(device_id):
    return SINGLE_PUNCH_CORRECTION_BY_DEVICE.get(str(device_id), True) is not False

def track_real_punch(employee_field_value, timestamp, device, context):
    """Keep unmatched real punches until a pair arrives or the correction deadline passes."""
    direction = context.get('direction')
    shift_name = context.get('shift_name')
    assignment_date = context.get('assignment_date')
    if (not AUTO_CORRECT_SINGLE_PUNCHES or not _device_correction_enabled(device.get('device_id'))
            or direction not in ('IN', 'OUT') or not shift_name):
        return

    key = _pending_key(employee_field_value, shift_name, assignment_date)
    with PENDING_PUNCH_LOCK:
        pending = _load_pending_punches()
        record = pending.get(key, {
            'employee_field_value': str(employee_field_value),
            'shift_name': shift_name,
            'assignment_date': assignment_date.isoformat(),
            'device_id': device.get('device_id'),
            'ip': device.get('ip'),
            'in_time': None,
            'out_time': None,
        })
        field = 'in_time' if direction == 'IN' else 'out_time'
        current = _safe_convert_date(record.get(field), '%Y-%m-%d %H:%M:%S')
        clean_timestamp = timestamp.replace(microsecond=0)
        if not current or (direction == 'IN' and clean_timestamp < current) or (direction == 'OUT' and clean_timestamp > current):
            record[field] = clean_timestamp.strftime('%Y-%m-%d %H:%M:%S')

        if record.get('in_time') and record.get('out_time'):
            pending.pop(key, None)
        else:
            pending[key] = record
        _save_pending_punches(pending)

def _add_automated_correction_comment(checkin_name, record, missing_direction, synthetic_time):
    content = (
        '<b>Automated biometric correction</b><br>'
        f'Missing {missing_direction} was issued at the scheduled shift boundary '
        f'{synthetic_time.strftime("%Y-%m-%d %H:%M:%S")}. '
        f'Source device: {record.get("device_id")}. Review this record if the employee worked different hours.'
    )
    try:
        response = requests.post(
            f'{config.ERPNEXT_URL}/api/resource/Comment',
            headers=_erp_headers(),
            json={
                'comment_type': 'Comment',
                'reference_doctype': 'Employee Checkin',
                'reference_name': checkin_name,
                'content': content,
            },
            timeout=30,
        )
        if response.status_code not in (200, 201):
            info_logger.warning(f'Correction comment could not be added to Employee Checkin {checkin_name}.')
    except Exception:
        info_logger.warning(f'Correction comment could not be added to Employee Checkin {checkin_name}.')

def reconcile_pending_punches(now=None, date_from=None, date_to=None, device_ids=None):
    """Create a clearly labelled boundary punch only after a shift's grace period has ended."""
    if not AUTO_CORRECT_SINGLE_PUNCHES:
        return []
    now = (now or datetime.datetime.now()).replace(microsecond=0)
    corrections = []

    with PENDING_PUNCH_LOCK:
        pending = _load_pending_punches()
        changed = False
        for key, record in list(pending.items()):
            if not _device_correction_enabled(record.get('device_id')):
                continue
            if device_ids is not None and str(record.get('device_id')) not in device_ids:
                continue
            shift_name = record.get('shift_name')
            bounds = SHIFT_BOUNDARIES.get(shift_name, {})
            assignment_date = _safe_convert_date(record.get('assignment_date'), '%Y-%m-%d')
            if not assignment_date or not bounds.get('start') or not bounds.get('end'):
                continue
            if date_from and assignment_date.date() < date_from:
                continue
            if date_to and assignment_date.date() > date_to:
                continue

            shift_start, shift_end = _shift_datetimes_for_assignment_date(
                shift_name, assignment_date.date()
            )
            grace_minutes = int(SINGLE_PUNCH_GRACE_BY_SHIFT.get(shift_name, 120))
            if not shift_end or now < shift_end + datetime.timedelta(minutes=grace_minutes):
                continue

            has_in = bool(record.get('in_time'))
            has_out = bool(record.get('out_time'))
            if has_in == has_out:
                pending.pop(key, None)
                changed = True
                continue

            missing_direction = 'OUT' if has_in else 'IN'
            synthetic_time = shift_end if missing_direction == 'OUT' else shift_start
            try:
                employee = _get_employee_by_attendance_device_id(record['employee_field_value'])
                if not employee:
                    raise Exception('Employee lookup failed for pending correction.')
                if not _ensure_temporary_shift_assignment(employee, shift_name, assignment_date.date()):
                    raise Exception('Shift Assignment could not be confirmed for pending correction.')
                correction_device_id = f'AUTO-CORRECTION:{record.get("device_id")}:{missing_direction}'
                status_code, message = send_to_erpnext(
                    record['employee_field_value'],
                    synthetic_time,
                    correction_device_id,
                    missing_direction,
                )
                duplicate = DUPLICATE_EMPLOYEE_CHECKIN_ERROR_MESSAGE in str(message)
                if status_code == 200:
                    _add_automated_correction_comment(message, record, missing_direction, synthetic_time)
                    correction_logger.info('\t'.join([
                        message,
                        record['employee_field_value'],
                        shift_name,
                        assignment_date.strftime('%Y-%m-%d'),
                        missing_direction,
                        synthetic_time.strftime('%Y-%m-%d %H:%M:%S'),
                        str(record.get('device_id')),
                    ]))
                    corrections.append({
                        'checkin': message,
                        'shift': shift_name,
                        'direction': missing_direction,
                        'time': synthetic_time.strftime('%Y-%m-%d %H:%M:%S'),
                        'device_id': record.get('device_id'),
                    })
                    pending.pop(key, None)
                    changed = True
                elif duplicate:
                    correction_logger.info('\t'.join([
                        'Already exists',
                        record['employee_field_value'],
                        shift_name,
                        assignment_date.strftime('%Y-%m-%d'),
                        missing_direction,
                        synthetic_time.strftime('%Y-%m-%d %H:%M:%S'),
                        str(record.get('device_id')),
                    ]))
                    pending.pop(key, None)
                    changed = True
            except Exception:
                error_logger.exception(f'Unable to correct pending punch {key}.')

        if changed:
            _save_pending_punches(pending)
    return corrections


# possible area of further developemt
# Real-time events - setup getting events pushed from the machine rather then polling.
# this is documented as 'Real-time events' in the ZKProtocol manual.

# Notes:
# Status Keys in status.json
# - lift_off_timestamp
# - mission_accomplished_timestamp
# - <device_id>_pull_timestamp
# - <device_id>_push_timestamp
# - <shift_type>_sync_timestamp

def main():
    try:
        last_lift_off_timestamp = _safe_convert_date(status.get('lift_off_timestamp'), "%Y-%m-%d %H:%M:%S.%f")
        if RANGE_SYNC or (last_lift_off_timestamp and last_lift_off_timestamp < datetime.datetime.now() - datetime.timedelta(minutes=config.PULL_FREQUENCY)) or not last_lift_off_timestamp:
            with ERP_CACHE_LOCK:
                EMPLOYEE_LOOKUP_CACHE.clear()
                SHIFT_ASSIGNMENT_CACHE.clear()
                TEMP_SHIFT_ASSIGNMENT_CACHE.clear()
            selected_devices = [
                device for device in config.devices
                if not REQUESTED_DEVICE_IDS or str(device.get('device_id')) in REQUESTED_DEVICE_IDS
            ]
            if not selected_devices:
                raise RuntimeError('No matching configured devices were selected for synchronization.')
            status.set('lift_off_timestamp', str(datetime.datetime.now()))
            range_start = _safe_convert_date(IMPORT_START_DATE, "%Y%m%d")
            range_end = _safe_convert_date(IMPORT_END_DATE, "%Y%m%d")
            if range_end:
                range_end = range_end + datetime.timedelta(days=1) - datetime.timedelta(microseconds=1)
            status.set('sync_progress', {
                'state': 'running', 'from': IMPORT_START_DATE, 'to': IMPORT_END_DATE,
                'processed': 0, 'uploaded': 0, 'skipped': 0, 'failed': 0,
                'devices_total': len(selected_devices), 'devices_done': 0,
                'started_at': str(datetime.datetime.now()),
                'current_device': None,
                'devices': {
                    device['device_id']: {
                        'device_id': device['device_id'],
                        'index': index + 1,
                        'state': 'waiting',
                        'processed': 0,
                        'uploaded': 0,
                        'skipped': 0,
                        'failed': 0,
                        'records_total': 0,
                        'current_date': None,
                        'from': IMPORT_START_DATE,
                        'to': IMPORT_END_DATE,
                    }
                    for index, device in enumerate(selected_devices)
                },
            })
            status.save()
            info_logger.info("Cleared for lift off!")
            def process_device(device):
                device_attendance_logs = None
                info_logger.info("Processing Device: " + device['device_id'])
                _update_sync_progress(device_id=device['device_id'], device_state='running')
                dump_file = get_dump_file_name_and_directory(device['device_id'], device['ip'])
                if os.path.exists(dump_file) and not RANGE_SYNC:
                    info_logger.error('Device Attendance Dump Found in Log Directory. This can mean the program crashed unexpectedly. Retrying with dumped data.')
                    with open(dump_file, 'r') as f:
                        file_contents = f.read()
                        if file_contents:
                            device_attendance_logs = list(map(lambda x: _apply_function_to_key(x, 'timestamp', datetime.datetime.fromtimestamp), json.loads(file_contents)))
                elif os.path.exists(dump_file) and RANGE_SYNC:
                    info_logger.warning(
                        'Range sync will fetch fresh device data instead of replaying a possibly stale recovery dump: '
                        + dump_file
                    )
                try:
                    pull_process_and_push_data(device, device_attendance_logs)
                    with STATUS_LOCK:
                        status.set(f'{device["device_id"]}_push_timestamp', str(datetime.datetime.now()))
                        status.save()
                    if os.path.exists(dump_file):
                        os.remove(dump_file)
                    info_logger.info("Successfully processed Device: " + device['device_id'])
                except:
                    _update_sync_progress(
                        failed=1,
                        device_done=True,
                        device_id=device['device_id'],
                    )
                    error_logger.exception('exception when calling pull_process_and_push_data function for device' + json.dumps(device, default=str))
            with ThreadPoolExecutor(max_workers=min(8, len(selected_devices)), thread_name_prefix='device-sync') as executor:
                futures = [executor.submit(process_device, device) for device in selected_devices]
                for future in as_completed(futures):
                    future.result()
            corrections = reconcile_pending_punches()
            if corrections:
                info_logger.info(f'Created {len(corrections)} automated single-punch correction(s).')
            if hasattr(config, 'shift_type_device_mapping'):
                update_shift_last_sync_timestamp(config.shift_type_device_mapping)
            status.set('mission_accomplished_timestamp', str(datetime.datetime.now()))
            progress = status.get('sync_progress') or {}
            progress['state'] = 'completed' if int(progress.get('failed', 0)) == 0 else 'completed_with_errors'
            progress['completed_at'] = str(datetime.datetime.now())
            status.set('sync_progress', progress)
            status.save()
            info_logger.info("Mission Accomplished!")
    except:
        progress = status.get('sync_progress') or {}
        progress['state'] = 'completed_with_errors'
        progress['completed_at'] = str(datetime.datetime.now())
        progress['failed'] = max(1, int(progress.get('failed', 0)))
        status.set('sync_progress', progress)
        status.save()
        error_logger.exception('exception has occurred in the main function...')

def pull_process_and_push_data(device, device_attendance_logs=None):
    attendance_success_log_file = '_'.join(["attendance_success_log", device['device_id']])
    attendance_failed_log_file = '_'.join(["attendance_failed_log", device['device_id']])
    attendance_success_logger = setup_logger(attendance_success_log_file, '/'.join([config.LOGS_DIRECTORY, attendance_success_log_file]) + '.log')
    attendance_failed_logger = setup_logger(attendance_failed_log_file, '/'.join([config.LOGS_DIRECTORY, attendance_failed_log_file]) + '.log')
    if not device_attendance_logs:
        device_attendance_logs = get_all_attendance_from_device(device['ip'], device_id=device['device_id'], clear_from_device_on_fetch=device['clear_from_device_on_fetch'])
        if not device_attendance_logs:
            _update_sync_progress(device_done=True, device_id=device['device_id'])
            return

    range_start = _safe_convert_date(IMPORT_START_DATE, "%Y%m%d")
    range_end = _safe_convert_date(IMPORT_END_DATE, "%Y%m%d")
    if range_end:
        range_end = range_end + datetime.timedelta(days=1) - datetime.timedelta(microseconds=1)
    selected_logs = _select_range_logs(device_attendance_logs, range_start, range_end)
    index_of_last = -1
    last_line = get_last_line_from_file('/'.join([config.LOGS_DIRECTORY, attendance_success_log_file]) + '.log')
    import_start_date = _safe_convert_date(config.IMPORT_START_DATE, "%Y%m%d")
    if not RANGE_SYNC and (last_line or import_start_date):
        last_user_id = None
        last_timestamp = None
        if last_line:
            last_user_id, last_timestamp = last_line.split("\t")[4:6]
            last_timestamp = datetime.datetime.fromtimestamp(float(last_timestamp))
        if import_start_date:
            if last_timestamp:
                if last_timestamp < import_start_date:
                    last_timestamp = import_start_date
                    last_user_id = None
            else:
                last_timestamp = import_start_date
        for i, x in enumerate(device_attendance_logs):
            if last_user_id and last_timestamp:
                if last_user_id == str(x['user_id']) and last_timestamp == x['timestamp']:
                    index_of_last = i
                    break
            elif last_timestamp:
                if x['timestamp'] >= last_timestamp:
                    index_of_last = i
                    break

    records_to_process = selected_logs if RANGE_SYNC else device_attendance_logs[index_of_last + 1:]
    record_dates = [record['timestamp'] for record in records_to_process if record.get('timestamp')]
    _update_sync_progress(
        device_id=device['device_id'],
        device_state='running',
        records_total=len(records_to_process),
        device_from=min(record_dates).strftime('%Y%m%d') if record_dates else IMPORT_START_DATE,
        device_to=max(record_dates).strftime('%Y%m%d') if record_dates else IMPORT_END_DATE,
        current_date=min(record_dates).strftime('%Y%m%d') if record_dates else None,
    )
    for device_attendance_log in records_to_process:
        try:
            punch_context = resolve_punch_context(device, device_attendance_log)
            punch_direction = punch_context['direction']
            erpnext_status_code, erpnext_message = send_to_erpnext(
                device_attendance_log['user_id'],
                device_attendance_log['timestamp'],
                device['device_id'],
                punch_direction,
                latitude=device.get('latitude'),
                longitude=device.get('longitude')
            )
            is_allowlisted = any(error in erpnext_message for error in allowlisted_errors)
            is_duplicate = DUPLICATE_EMPLOYEE_CHECKIN_ERROR_MESSAGE in erpnext_message
            if erpnext_status_code == 200 or is_allowlisted:
                if erpnext_status_code != 200:
                    info_logger.warning("\t".join([
                        "Allowed ERPNext response; punch checkpoint advanced",
                        str(erpnext_status_code),
                        str(device.get('device_id')),
                        str(device_attendance_log['user_id']),
                        str(device_attendance_log['timestamp']),
                    ]))
                if erpnext_status_code == 200 or is_duplicate:
                    track_real_punch(
                        device_attendance_log['user_id'],
                        device_attendance_log['timestamp'],
                        device,
                        punch_context,
                    )
                checkpoint_message = erpnext_message if erpnext_status_code == 200 else f'IGNORED:{erpnext_status_code}'
                attendance_success_logger.info("\t".join([
                    checkpoint_message,
                    str(device_attendance_log['uid']),
                    str(device_attendance_log['user_id']),
                    str(device_attendance_log['timestamp'].timestamp()),
                    str(device_attendance_log['punch']),
                    str(device_attendance_log['status']),
                    json.dumps(device_attendance_log, default=str)
                ]))
                _update_sync_progress(
                    processed=1,
                    uploaded=1 if erpnext_status_code == 200 else 0,
                    skipped=0 if erpnext_status_code == 200 else 1,
                    device_id=device['device_id'],
                    current_date=device_attendance_log['timestamp'].strftime('%Y%m%d'),
                )
            else:
                attendance_failed_logger.error("\t".join([
                    'ACTIONABLE',
                    str(erpnext_status_code),
                    str(device_attendance_log['uid']),
                    str(device_attendance_log['user_id']),
                    str(device_attendance_log['timestamp'].timestamp()),
                    str(device_attendance_log['punch']),
                    str(device_attendance_log['status']),
                    json.dumps(device_attendance_log, default=str)
                ]))
                _update_sync_progress(
                    processed=1,
                    failed=1,
                    device_id=device['device_id'],
                    current_date=device_attendance_log['timestamp'].strftime('%Y%m%d'),
                )
                # A rejected punch must not prevent later employees at this
                # location from reaching ERPNext.
                continue
        except Exception as exc:
            attendance_failed_logger.error("\t".join([
                'PROCESSING_ERROR',
                type(exc).__name__,
                str(device_attendance_log.get('uid', '')),
                str(device_attendance_log.get('user_id', '')),
                str(device_attendance_log.get('timestamp', '')),
                str(device_attendance_log.get('punch', '')),
                str(device_attendance_log.get('status', '')),
                str(exc),
            ]))
            error_logger.exception(
                'Punch skipped; continuing device=%s employee=%s timestamp=%s',
                device.get('device_id'),
                device_attendance_log.get('user_id'),
                device_attendance_log.get('timestamp'),
            )
            punch_timestamp = device_attendance_log.get('timestamp')
            _update_sync_progress(
                processed=1,
                failed=1,
                device_id=device['device_id'],
                current_date=punch_timestamp.strftime('%Y%m%d') if punch_timestamp else None,
            )
            continue
    _update_sync_progress(device_done=True, device_id=device['device_id'])


def _select_range_logs(records, range_start, range_end):
    return [
        record for record in records
        if (not range_start or record['timestamp'] >= range_start)
        and (not range_end or record['timestamp'] <= range_end)
    ]


def _update_sync_progress(
    processed=0, uploaded=0, skipped=0, failed=0, device_done=False,
    device_id=None, current_date=None, records_total=None, device_state=None,
    device_from=None, device_to=None,
):
    with STATUS_LOCK:
        progress = status.get('sync_progress') or {}
        progress['processed'] = int(progress.get('processed', 0)) + processed
        progress['uploaded'] = int(progress.get('uploaded', 0)) + uploaded
        progress['skipped'] = int(progress.get('skipped', 0)) + skipped
        progress['failed'] = int(progress.get('failed', 0)) + failed
        if device_done:
            progress['devices_done'] = int(progress.get('devices_done', 0)) + 1
        if device_id:
            devices = progress.setdefault('devices', {})
            device_progress = devices.setdefault(device_id, {
            'device_id': device_id, 'state': 'waiting', 'processed': 0,
            'uploaded': 0, 'skipped': 0, 'failed': 0, 'records_total': 0,
        })
            device_progress['processed'] = int(device_progress.get('processed', 0)) + processed
            device_progress['uploaded'] = int(device_progress.get('uploaded', 0)) + uploaded
            device_progress['skipped'] = int(device_progress.get('skipped', 0)) + skipped
            device_progress['failed'] = int(device_progress.get('failed', 0)) + failed
            if records_total is not None:
                device_progress['records_total'] = max(0, int(records_total))
            if current_date:
                device_progress['current_date'] = current_date
            if device_from:
                device_progress['from'] = device_from
            if device_to:
                device_progress['to'] = device_to
            if device_state:
                device_progress['state'] = device_state
            if device_done:
                device_progress['state'] = 'completed_with_errors' if int(device_progress.get('failed', 0)) else 'completed'
                device_progress['completed_at'] = str(datetime.datetime.now())
            elif device_progress.get('state') == 'running':
                progress['current_device'] = device_id
            devices[device_id] = device_progress
            progress['devices'] = devices
        status.set('sync_progress', progress)
        status.save()

def get_all_attendance_from_device(ip, port=4370, timeout=30, device_id=None, clear_from_device_on_fetch=False):
    # The application runs as an unprivileged service account in Linux/LXC.
    # pyzk's preliminary ICMP probe requires privileges that account may not
    # have, even though the terminal's ZKTeco service is reachable on port 4370.
    # Connect directly and let the protocol exchange determine availability.
    zk = ZK(ip, port=port, timeout=timeout, ommit_ping=True)
    conn = None
    attendances = []
    try:
        conn = zk.connect()
        if SYNC_DEVICE_TIME_WITH_PC:
            pc_time = datetime.datetime.now().replace(microsecond=0)
            conn.set_time(pc_time)
            info_logger.info("\t".join((ip, "Device clock synchronized with PC:", str(pc_time))))
        x = conn.disable_device()
        info_logger.info("\t".join((ip, "Device Disable Attempted. Result:", str(x))))
        attendances = conn.get_attendance()
        info_logger.info("\t".join((ip, "Attendances Fetched:", str(len(attendances)))))
        with STATUS_LOCK:
            status.set(f'{device_id}_push_timestamp', None)
            status.set(f'{device_id}_pull_timestamp', str(datetime.datetime.now()))
            status.save()
        if len(attendances):
            dump_file_name = get_dump_file_name_and_directory(device_id, ip)
            with open(dump_file_name, 'w+') as f:
                f.write(json.dumps(list(map(lambda x: x.__dict__, attendances)), default=datetime.datetime.timestamp))
            if clear_from_device_on_fetch:
                x = conn.clear_attendance()
                info_logger.info("\t".join((ip, "Attendance Clear Attempted. Result:", str(x))))
        x = conn.enable_device()
        info_logger.info("\t".join((ip, "Device Enable Attempted. Result:", str(x))))
    except:
        error_logger.exception(str(ip) + ' exception when fetching from device...')
        raise Exception('Device fetch failed.')
    finally:
        if conn:
            conn.disconnect()
    return list(map(lambda x: x.__dict__, attendances))

def send_to_erpnext(employee_field_value, timestamp, device_id=None, log_type=None, latitude=None, longitude=None):
    endpoint_app = "hrms" if ERPNEXT_VERSION > 13 else "erpnext"
    url = f"{config.ERPNEXT_URL}/api/method/{endpoint_app}.hr.doctype.employee_checkin.employee_checkin.add_log_based_on_employee_field"
    data = {
        'employee_field_value': employee_field_value,
        'timestamp': timestamp.__str__(),
        'device_id': device_id,
        'log_type': log_type,
        'latitude': latitude,
        'longitude': longitude
    }
    response = _http_session().post(url, json=data, timeout=30)
    if response.status_code == 200:
        return 200, json.loads(response._content)['message']['name']
    else:
        error_str = _safe_get_error_str(response)
        if any(message in error_str for message in allowlisted_errors):
            info_logger.warning('\t'.join([
                'ERPNext response safely skipped',
                str(response.status_code),
                str(employee_field_value),
                str(timestamp),
                str(device_id),
                str(log_type),
            ]))
        else:
            error_logger.error('\t'.join(['Error during ERPNext API Call.', str(employee_field_value), str(timestamp.timestamp()), str(device_id), str(log_type), error_str]))
        return response.status_code, error_str

def update_shift_last_sync_timestamp(shift_type_device_mapping):
    for shift_type_device_map in shift_type_device_mapping:
        all_devices_pushed = True
        pull_timestamp_array = []
        for device_id in shift_type_device_map['related_device_id']:
            if not status.get(f'{device_id}_push_timestamp'):
                all_devices_pushed = False
                break
            pull_timestamp_array.append(_safe_convert_date(status.get(f'{device_id}_pull_timestamp'), "%Y-%m-%d %H:%M:%S.%f"))
        if all_devices_pushed:
            min_pull_timestamp = min(pull_timestamp_array)
            if isinstance(shift_type_device_map['shift_type_name'], str):
                shift_type_device_map['shift_type_name'] = [shift_type_device_map['shift_type_name']]
            for shift in shift_type_device_map['shift_type_name']:
                try:
                    sync_current_timestamp = _safe_convert_date(status.get(f'{shift}_sync_timestamp'), "%Y-%m-%d %H:%M:%S.%f")
                    if (sync_current_timestamp and min_pull_timestamp > sync_current_timestamp) or (min_pull_timestamp and not sync_current_timestamp):
                        response_code = send_shift_sync_to_erpnext(shift, min_pull_timestamp)
                        if response_code == 200:
                            status.set(f'{shift}_sync_timestamp', str(min_pull_timestamp))
                            status.save()
                except:
                    error_logger.exception('Exception in update_shift_last_sync_timestamp, for shift:' + shift)

def send_shift_sync_to_erpnext(shift_type_name, sync_timestamp):
    url = config.ERPNEXT_URL + "/api/resource/Shift Type/" + shift_type_name
    headers = {
        'Authorization': "token " + config.ERPNEXT_API_KEY + ":" + config.ERPNEXT_API_SECRET,
        'Accept': 'application/json'
    }
    data = {
        "last_sync_of_checkin": str(sync_timestamp)
    }
    try:
        response = requests.request("PUT", url, headers=headers, data=json.dumps(data))
        if response.status_code == 200:
            info_logger.info("\t".join(['Shift Type last_sync_of_checkin Updated', str(shift_type_name), str(sync_timestamp.timestamp())]))
        else:
            error_str = _safe_get_error_str(response)
            error_logger.error('\t'.join(['Error during ERPNext Shift Type API Call.', str(shift_type_name), str(sync_timestamp.timestamp()), error_str]))
        return response.status_code
    except:
        error_logger.exception("\t".join(['exception when updating last_sync_of_checkin in Shift Type', str(shift_type_name), str(sync_timestamp.timestamp())]))

def get_last_line_from_file(file):
    line = None
    if os.stat(file).st_size < 5000:
        with open(file, 'r') as f:
            for line in f:
                pass
    else:
        with open(file, 'rb') as f:
            f.seek(-2, os.SEEK_END)
            while f.read(1) != b'\n':
                f.seek(-2, os.SEEK_CUR)
            line = f.readline().decode()
    return line

def setup_logger(name, log_file, level=logging.INFO, formatter=None):
    if not formatter:
        formatter = logging.Formatter('%(asctime)s\t%(levelname)s\t%(message)s')

    # Keep useful history without allowing unattended installations to fill the disk.
    handler = RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=10)
    handler.setFormatter(formatter)

    logger = logging.getLogger(name)
    logger.setLevel(level)
    if not logger.hasHandlers():
        logger.addHandler(handler)

    return logger

def get_dump_file_name_and_directory(device_id, device_ip):
    return config.LOGS_DIRECTORY + '/' + device_id + "_" + device_ip.replace('.', '_') + '_last_fetch_dump.json'

def _apply_function_to_key(obj, key, fn):
    obj[key] = fn(obj[key])
    return obj

def _safe_convert_date(datestring, pattern):
    try:
        return datetime.datetime.strptime(datestring, pattern)
    except:
        return None

def _safe_get_error_str(res):
    try:
        error_json = json.loads(res._content)
        if 'exc' in error_json:
            error_str = json.loads(error_json['exc'])[0]
        else:
            error_str = json.dumps(error_json)
    except:
        error_str = str(res.__dict__)
    return error_str

if not os.path.exists(config.LOGS_DIRECTORY):
    os.makedirs(config.LOGS_DIRECTORY)
error_logger = setup_logger('error_logger', '/'.join([config.LOGS_DIRECTORY, 'error.log']), logging.ERROR)
info_logger = setup_logger('info_logger', '/'.join([config.LOGS_DIRECTORY, 'logs.log']))
correction_logger = setup_logger('correction_logger', '/'.join([config.LOGS_DIRECTORY, 'automated_corrections.log']))
status = PickleDB('/'.join([config.LOGS_DIRECTORY, 'status.json']))

def infinite_loop(sleep_time=15):
    print("Service Running...")
    while True:
        try:
            main()
            time.sleep(sleep_time)
        except BaseException as e:
            print(e)

if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == '--correct-range':
        range_from = datetime.datetime.strptime(sys.argv[2], '%Y-%m-%d').date()
        range_to = datetime.datetime.strptime(sys.argv[3], '%Y-%m-%d').date()
        selected_devices = set(json.loads(sys.argv[4])) if len(sys.argv) >= 5 else None
        print(json.dumps(reconcile_pending_punches(date_from=range_from, date_to=range_to, device_ids=selected_devices), default=str))
    else:
        infinite_loop()
