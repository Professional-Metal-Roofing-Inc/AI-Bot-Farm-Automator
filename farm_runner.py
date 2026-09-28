"""Run the reply bot across a farm of Android phones, round after round.

This is the rota. It reads phones.json, then for each enabled phone starts
draft_reply.py as a separate process with DEVICE_SERIAL and PHONE_NAME set,
so one wedged phone cannot take down the round. After every phone has been
visited it waits a random 10-20 minutes and goes again.

Responsibilities that live here rather than in the bot: the lock file that
keeps two rotas from driving the same phones, per-phone timeouts (killing the
adb and uiautomator2 helpers the phone left behind), and the result files
farm_state.json, farm_service_results.json and farm_check_results.json.

    --watch          the rota; --cycles 1 for a single pass
    (no flag)        navigation-only readiness check of every enabled phone
    --phone SERIAL   limit either of the above to one phone
    --discover       rebuild phones.json from the plugged-in phones

README.md documents the settings; TROUBLESHOOTING.md covers what the
failures mean.
"""
import argparse
import contextlib
import json
import os
import random
import signal
import subprocess
import sys
import time
from pathlib import Path


BASE_PATH = Path(__file__).resolve().parent
CONFIG_PATH = BASE_PATH / "phones.json"
RESULTS_PATH = BASE_PATH / "farm_check_results.json"
SERVICE_RESULTS_PATH = BASE_PATH / "farm_service_results.json"
STATE_PATH = BASE_PATH / "farm_state.json"
LOCK_PATH = BASE_PATH / "farm_runner.lock"
DEFAULT_DRAFT_SCRIPT = BASE_PATH / "draft_reply.py"
DEFAULT_SERVICE_TIMEOUT_SECONDS = 600
STDERR_CAPTURE_BYTES = 2048


def write_json(path, value):
    """Write JSON atomically, leaving no partial file behind on failure."""
    temporary_path = path.with_suffix(path.suffix + ".tmp")

    try:
        with open(
            temporary_path,
            "w",
            encoding="utf-8",
            newline="\n",
        ) as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(temporary_path, path)
    except Exception:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass

        raise


def process_is_running(pid):
    """Report whether a PID from a lock file still belongs to a process."""
    if not isinstance(pid, int) or pid <= 0:
        return False

    if os.name == "nt":
        # os.kill(pid, 0) terminates the target on Windows, so ask the
        # process table instead.
        try:
            completed = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            return True

        return str(pid) in (completed.stdout or "")

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True

    return True


@contextlib.contextmanager
def farm_runner_lock():
    """Guarantee that only one runner drives the farm at a time."""
    if LOCK_PATH.exists():
        holder = {}

        try:
            parsed = json.loads(LOCK_PATH.read_text(encoding="utf-8"))

            if isinstance(parsed, dict):
                holder = parsed
        except (json.JSONDecodeError, OSError):
            holder = {}

        holder_pid = holder.get("pid")

        if process_is_running(holder_pid):
            raise RuntimeError(
                f"Another farm_runner.py is already running (PID "
                f"{holder_pid}, started {holder.get('started_at', 'unknown')}, "
                f"command {holder.get('command', 'unknown')!r}). Stop it "
                f"first, or delete {LOCK_PATH.name} if it is stale."
            )

        print(
            f"Removing a stale lock file left by PID {holder_pid!r}: "
            f"{LOCK_PATH.name}"
        )

    write_json(
        LOCK_PATH,
        {
            "pid": os.getpid(),
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "command": " ".join(sys.argv[1:]),
        },
    )

    try:
        yield
    finally:
        try:
            current = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            current = None

        if isinstance(current, dict) and current.get("pid") == os.getpid():
            try:
                LOCK_PATH.unlink()
            except FileNotFoundError:
                pass


def decode_stderr_tail(stderr_bytes):
    """Keep the last few kilobytes of a child's stderr for diagnosis."""
    if not stderr_bytes:
        return ""

    tail = stderr_bytes[-STDERR_CAPTURE_BYTES:]
    text = tail.decode("utf-8", errors="replace").strip()

    if len(stderr_bytes) > STDERR_CAPTURE_BYTES:
        text = f"...(truncated)...\n{text}"

    return text


def terminate_process_tree(process):
    """Kill the child and the adb/uiautomator2 helpers it spawned."""
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except OSError:
            process.kill()

        return

    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.kill()
        except ProcessLookupError:
            pass


def run_phone_script(draft_script, option, environment, timeout):
    """Run one phone script in its own process group, capturing stderr.

    subprocess.run(timeout=...) only kills the direct child, so a timed-out
    cycle used to leave uiautomator2 and adb helpers running. The child is
    started in its own process group so the whole tree can be killed.
    """
    if os.name == "nt":
        # taskkill /T walks the parent/child tree, so no separate process
        # group is needed. Creating one would also stop Ctrl+C from
        # reaching the child.
        group_kwargs = {}
    else:
        group_kwargs = {"start_new_session": True}

    process = subprocess.Popen(
        [sys.executable, str(draft_script), option],
        cwd=str(draft_script.parent),
        env=environment,
        stderr=subprocess.PIPE,
        **group_kwargs,
    )
    timed_out = False

    try:
        _, stderr_bytes = process.communicate(
            timeout=timeout if timeout and timeout > 0 else None
        )
    except subprocess.TimeoutExpired:
        timed_out = True
        terminate_process_tree(process)

        try:
            _, stderr_bytes = process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            stderr_bytes = b""

    stderr_tail = decode_stderr_tail(stderr_bytes)

    if stderr_tail:
        print(stderr_tail, file=sys.stderr)

    return timed_out, process.returncode, stderr_tail


def discover_phones(replace=False):
    try:
        from adbutils import adb
    except ImportError as error:
        raise RuntimeError(
            "adbutils is missing. Run this with the same virtual environment "
            "used by draft_reply.py."
        ) from error

    if CONFIG_PATH.exists() and not replace:
        raise RuntimeError(
            f"{CONFIG_PATH.name} already exists. Edit it directly, or run "
            "--discover --replace to rebuild it."
        )

    discovered = []

    for device in sorted(adb.device_list(), key=lambda item: item.serial):
        try:
            state = device.get_state()
        except Exception as error:
            state = f"error: {error}"

        discovered.append(
            {
                "serial": device.serial,
                "name": f"Phone {len(discovered) + 1:02d}",
                "enabled": state == "device",
                "discovered_state": state,
            }
        )

    if not discovered:
        raise RuntimeError("No Android devices were discovered.")

    config = {
        "phones": discovered,
    }
    write_json(CONFIG_PATH, config)

    online_count = sum(
        1 for phone in discovered if phone["enabled"]
    )
    print(f"Created: {CONFIG_PATH}")
    print(f"Discovered phones: {len(discovered)}")
    print(f"Enabled and online: {online_count}")
    print(
        "Review phones.json before running checks. You can rename phones "
        "or set enabled to false."
    )


def phone_is_enabled(raw_phone, index):
    """Read the enabled flag, accepting hand-edited non-boolean values."""
    raw_value = raw_phone.get("enabled", True)

    if isinstance(raw_value, bool):
        return raw_value

    if raw_value is None:
        return True

    normalized = str(raw_value).strip().casefold()

    if normalized in {"1", "true", "yes", "on"}:
        enabled = True
    elif normalized in {"0", "false", "no", "off"}:
        enabled = False
    else:
        raise RuntimeError(
            f"Phone entry {index} has an unrecognized enabled value: "
            f"{raw_value!r}. Use true or false."
        )

    print(
        f"Warning: phone entry {index} has a non-boolean enabled value "
        f"{raw_value!r}; treating it as {str(enabled).lower()}."
    )
    return enabled


def load_enabled_phones(selected_serial=None):
    if not CONFIG_PATH.exists():
        raise RuntimeError(
            f"Missing {CONFIG_PATH.name}. Run farm_runner.py --discover first."
        )

    try:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"{CONFIG_PATH.name} is not valid JSON: {error}"
        ) from error

    if not isinstance(config, dict):
        raise RuntimeError(
            f"{CONFIG_PATH.name} must contain a JSON object with a phones "
            "list."
        )

    raw_phones = config.get("phones")

    if not isinstance(raw_phones, list):
        raise RuntimeError(
            f"{CONFIG_PATH.name} must contain a phones list."
        )

    phones = []
    seen_serials = set()

    for index, raw_phone in enumerate(raw_phones, start=1):
        if not isinstance(raw_phone, dict):
            raise RuntimeError(
                f"Phone entry {index} must be a JSON object."
            )

        serial = str(raw_phone.get("serial", "")).strip()

        if not serial:
            raise RuntimeError(f"Phone entry {index} has no serial.")

        if serial in seen_serials:
            raise RuntimeError(f"Duplicate phone serial: {serial}")

        seen_serials.add(serial)

        if not phone_is_enabled(raw_phone, index):
            continue

        if selected_serial and serial != selected_serial:
            continue

        name = str(raw_phone.get("name") or "").strip() or (
            f"Phone {index:02d}"
        )
        phones.append(
            {
                "serial": serial,
                "name": name,
            }
        )

    if selected_serial and not phones:
        raise RuntimeError(
            f"Enabled phone {selected_serial!r} was not found in "
            f"{CONFIG_PATH.name}."
        )

    if not phones:
        raise RuntimeError("No enabled phones were found in phones.json.")

    return phones


def save_results(results, draft_script):
    passed = sum(1 for result in results if result["status"] == "passed")
    timed_out = sum(1 for result in results if result["status"] == "timeout")
    failed = len(results) - passed - timed_out
    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "draft_script": str(draft_script),
        "total": len(results),
        "passed": passed,
        "failed": failed,
        "timed_out": timed_out,
        "results": results,
    }
    write_json(RESULTS_PATH, report)
    return passed, failed, timed_out


def check_farm(phones, draft_script, timeout, pause):
    if not draft_script.exists():
        raise RuntimeError(f"Missing draft script: {draft_script}")

    results = []
    total = len(phones)

    for index, phone in enumerate(phones, start=1):
        serial = phone["serial"]
        name = phone["name"]
        print("\n" + "=" * 64)
        print(f"[{index}/{total}] {name} — {serial}")
        print("=" * 64)

        environment = os.environ.copy()
        environment["DEVICE_SERIAL"] = serial
        environment["PHONE_NAME"] = name
        environment["PYTHONIOENCODING"] = "utf-8"
        started_at = time.monotonic()
        status = "failed"
        return_code = None
        error_message = None
        stderr_tail = ""

        try:
            timed_out, return_code, stderr_tail = run_phone_script(
                draft_script,
                "--check-phone",
                environment,
                timeout,
            )

            if timed_out:
                status = "timeout"
                return_code = None
                error_message = (
                    f"Phone check exceeded the {timeout}-second timeout."
                )
                print(error_message)
            else:
                status = "passed" if return_code == 0 else "failed"
        except Exception as error:
            error_message = str(error)
            print(f"Runner error: {error}")

        duration_seconds = round(time.monotonic() - started_at, 2)
        results.append(
            {
                "serial": serial,
                "name": name,
                "status": status,
                "return_code": return_code,
                "duration_seconds": duration_seconds,
                "error": error_message,
                "stderr_tail": stderr_tail,
            }
        )

        save_results(results, draft_script)

        if index < total and pause > 0:
            time.sleep(pause)

    passed, failed, timed_out = save_results(results, draft_script)

    print("\n" + "=" * 64)
    print("FARM CHECK COMPLETE")
    print("=" * 64)
    print(f"Passed: {passed}")
    print(f"Failed: {failed}")
    print(f"Timed out: {timed_out}")
    print(f"Results: {RESULTS_PATH}")

    if failed or timed_out:
        raise SystemExit(1)


def load_watch_state():
    # watch_farm only carries last_serial forward; next_index was never read.
    empty_state = {"last_serial": None}

    if not STATE_PATH.exists():
        return dict(empty_state)

    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return dict(empty_state)

    if not isinstance(state, dict):
        return dict(empty_state)

    last_serial = state.get("last_serial")

    if not isinstance(last_serial, str) or not last_serial.strip():
        last_serial = None

    return {"last_serial": last_serial}


def append_service_result(result):
    history = []

    if SERVICE_RESULTS_PATH.exists():
        try:
            existing = json.loads(
                SERVICE_RESULTS_PATH.read_text(encoding="utf-8")
            )
            raw_history = (
                existing.get("results", [])
                if isinstance(existing, dict)
                else []
            )

            if isinstance(raw_history, list):
                history = raw_history
        except (json.JSONDecodeError, OSError):
            history = []

    history.append(result)
    history = history[-500:]
    report = {
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "results": history,
    }
    write_json(SERVICE_RESULTS_PATH, report)


def service_one_phone(phone, draft_script, timeout):
    serial = phone["serial"]
    name = phone["name"]
    environment = os.environ.copy()
    environment["DEVICE_SERIAL"] = serial
    environment["PHONE_NAME"] = name
    environment["PYTHONIOENCODING"] = "utf-8"
    started_at = time.monotonic()
    status = "failed"
    return_code = None
    error_message = None

    print("\n" + "=" * 64)
    print(f"SERVICE CYCLE — {name} — {serial}")
    print("=" * 64)

    stderr_tail = ""

    try:
        timed_out, return_code, stderr_tail = run_phone_script(
            draft_script,
            "--service-once",
            environment,
            timeout,
        )

        if timed_out:
            status = "timeout"
            return_code = None
            error_message = (
                f"Service cycle exceeded the {timeout}-second timeout."
            )
            print(error_message)
        else:
            status = "completed" if return_code == 0 else "failed"
    except Exception as error:
        error_message = str(error)
        print(f"Runner error: {error}")

    result = {
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "serial": serial,
        "name": name,
        "status": status,
        "return_code": return_code,
        "duration_seconds": round(time.monotonic() - started_at, 2),
        "error": error_message,
        "stderr_tail": stderr_tail,
    }
    append_service_result(result)
    return result


def watch_farm(
    selected_serial,
    draft_script,
    min_interval_minutes,
    max_interval_minutes,
    service_timeout,
    cycles,
):
    if not draft_script.exists():
        raise RuntimeError(f"Missing draft script: {draft_script}")

    state = load_watch_state()
    completed_rounds = 0

    print(
        f"Starting full-farm service loop. Every enabled phone will be "
        f"checked before a fresh random delay from "
        f"{min_interval_minutes} to {max_interval_minutes} minutes will "
        "be chosen."
    )
    initial_phones = load_enabled_phones(
        selected_serial=selected_serial
    )
    print(
        f"Enabled phones in rotation: {len(initial_phones)}. "
        f"Service script: {draft_script.name}."
    )
    if service_timeout > 0:
        print(
            f"Per-phone timeout: {service_timeout} seconds. A timed-out "
            "phone will be logged and the rotation will continue."
        )
    print("Press Ctrl+C to stop after the current console interaction.")

    try:
        while True:
            phones = load_enabled_phones(
                selected_serial=selected_serial
            )
            completed_rounds += 1
            round_started_at = time.strftime("%Y-%m-%dT%H:%M:%S")
            round_results = []

            print("\n" + "#" * 64)
            print(
                f"FARM ROUND {completed_rounds} — "
                f"{len(phones)} ENABLED PHONE(S)"
            )
            print("#" * 64)

            for phone_index, phone in enumerate(phones, start=1):
                print(
                    f"\nPhone {phone_index}/{len(phones)} in this round"
                )
                result = service_one_phone(
                    phone,
                    draft_script,
                    timeout=service_timeout,
                )
                round_results.append(result)
                state["last_serial"] = phone["serial"]
                state["last_status"] = result["status"]
                state["round_phone_index"] = phone_index
                state["round_phone_total"] = len(phones)
                state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                write_json(STATE_PATH, state)

            completed_count = sum(
                result["status"] == "completed"
                for result in round_results
            )
            failed_count = len(round_results) - completed_count
            state["completed_rounds"] = completed_rounds
            state["round_started_at"] = round_started_at
            state["round_completed_at"] = time.strftime(
                "%Y-%m-%dT%H:%M:%S"
            )
            state["round_completed"] = completed_count
            state["round_failed"] = failed_count
            state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            write_json(STATE_PATH, state)

            print("\n" + "=" * 64)
            print(f"FARM ROUND {completed_rounds} COMPLETE")
            print(f"Completed: {completed_count}")
            print(f"Failed or timed out: {failed_count}")
            print("=" * 64)

            if cycles > 0 and completed_rounds >= cycles:
                print(
                    f"Completed requested farm rounds: "
                    f"{completed_rounds}"
                )
                return

            delay_minutes = random.randint(
                min_interval_minutes,
                max_interval_minutes,
            )
            interval_seconds = delay_minutes * 60
            next_run_epoch = time.time() + interval_seconds
            next_run_text = time.strftime(
                "%Y-%m-%d %H:%M:%S",
                time.localtime(next_run_epoch),
            )
            state["next_delay_minutes"] = delay_minutes
            state["next_run_at"] = next_run_text
            write_json(STATE_PATH, state)
            print(
                f"Random delay selected: {delay_minutes} minute(s). "
                f"The next complete farm round will start at "
                f"approximately "
                f"{next_run_text}."
            )
            time.sleep(interval_seconds)
    except KeyboardInterrupt:
        print("\nService loop stopped.")


def parse_arguments():
    parser = argparse.ArgumentParser(
        description=(
            "Discover phone-farm devices, run Marketplace readiness "
            "checks, and monitor enabled phones one at a time."
        )
    )
    parser.add_argument(
        "--discover",
        action="store_true",
        help="Discover connected phones and create phones.json.",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Allow --discover to replace an existing phones.json.",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help=(
            "Service every enabled phone, then wait for one randomly "
            "selected interval before beginning the next full round."
        ),
    )
    parser.add_argument(
        "--phone",
        help=(
            "Limit checks or monitoring to one enabled serial from "
            "phones.json. Omit it to rotate through every enabled phone."
        ),
    )
    parser.add_argument(
        "--script",
        type=Path,
        default=DEFAULT_DRAFT_SCRIPT,
        help=(
            "Path to the phone service script (default: draft_reply.py)."
        ),
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=120,
        help="Maximum seconds allowed for each phone check.",
    )
    parser.add_argument(
        "--pause",
        type=float,
        default=1.0,
        help="Seconds to pause between phones.",
    )
    parser.add_argument(
        "--min-interval-minutes",
        type=int,
        default=10,
        help="Minimum random delay between watch cycles (default: 10).",
    )
    parser.add_argument(
        "--max-interval-minutes",
        type=int,
        default=20,
        help="Maximum random delay between watch cycles (default: 20).",
    )
    parser.add_argument(
        "--service-timeout",
        type=int,
        default=DEFAULT_SERVICE_TIMEOUT_SECONDS,
        help=(
            "Maximum seconds for one phone service cycle (default: 600); "
            "0 disables the timeout."
        ),
    )
    parser.add_argument(
        "--cycles",
        type=int,
        default=0,
        help="Stop after this many complete farm rounds; 0 runs until Ctrl+C.",
    )
    return parser.parse_args()


def main():
    arguments = parse_arguments()

    if arguments.replace and not arguments.discover:
        raise RuntimeError("--replace can only be used with --discover.")

    if arguments.discover and arguments.watch:
        raise RuntimeError("--discover and --watch cannot be used together.")

    if arguments.timeout < 10:
        raise RuntimeError("--timeout must be at least 10 seconds.")

    if arguments.pause < 0:
        raise RuntimeError("--pause cannot be negative.")

    if arguments.min_interval_minutes < 1:
        raise RuntimeError("--min-interval-minutes must be at least 1.")

    if arguments.max_interval_minutes < arguments.min_interval_minutes:
        raise RuntimeError(
            "--max-interval-minutes must be greater than or equal to "
            "--min-interval-minutes."
        )

    if arguments.service_timeout < 0:
        raise RuntimeError("--service-timeout cannot be negative.")

    if arguments.cycles < 0:
        raise RuntimeError("--cycles cannot be negative.")

    if arguments.discover:
        discover_phones(replace=arguments.replace)
        return

    draft_script = arguments.script.resolve()

    # Two runners driving the same phones would double-reply and clobber
    # farm_state.json, so only one may hold the lock.
    with farm_runner_lock():
        if arguments.watch:
            watch_farm(
                selected_serial=arguments.phone,
                draft_script=draft_script,
                min_interval_minutes=arguments.min_interval_minutes,
                max_interval_minutes=arguments.max_interval_minutes,
                service_timeout=arguments.service_timeout,
                cycles=arguments.cycles,
            )
            return

        phones = load_enabled_phones(selected_serial=arguments.phone)
        check_farm(
            phones,
            draft_script,
            timeout=arguments.timeout,
            pause=arguments.pause,
        )


if __name__ == "__main__":
    main()
