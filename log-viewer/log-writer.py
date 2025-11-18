#!/usr/bin/env python3
"""
Home Assistant Log File Writer
Replicates the exact log rotation logic that Home Assistant Core used before removal.
"""

import os
import sys
import subprocess
import signal
import re
from pathlib import Path
from logging.handlers import TimedRotatingFileHandler, RotatingFileHandler
import logging

# Configuration from environment (set by add-on)
LOG_FILE = os.getenv("LOG_FILE", "/config/home-assistant.log")
LOG_ROTATE_DAYS = os.getenv("LOG_ROTATE_DAYS")  # None or number
VERBOSE = os.getenv("VERBOSE", "false").lower() == "true"

# Convert to int or None
if LOG_ROTATE_DAYS:
    try:
        LOG_ROTATE_DAYS = int(LOG_ROTATE_DAYS)
    except (ValueError, TypeError):
        LOG_ROTATE_DAYS = None

class RotatingFileHandlerWithoutShouldRollOver(RotatingFileHandler):
    """
    Replica of Home Assistant's custom RotatingFileHandler.

    RotatingFileHandler that does not check if it should roll over on every log.
    The shouldRollover check is expensive because it has to stat the log file
    for every log record. Since we do not set maxBytes, the result of this
    check is always False.
    """
    def shouldRollover(self, record):
        """Never roll over.

        The shouldRollover check is expensive because it has to stat
        the log file for every log record. Since we do not set maxBytes
        the result of this check is always False.
        """
        return False


def strip_ansi_codes(text):
    """
    Remove ANSI escape sequences (color codes) from text.
    
    This strips all ANSI escape sequences including:
    - Color codes: \x1b[31m, \x1b[0m, etc.
    - Other control sequences: \x1b[K, \x1b[2J, etc.
    
    Args:
        text: String that may contain ANSI escape sequences
        
    Returns:
        String with ANSI escape sequences removed
    """
    # Pattern matches ANSI escape sequences:
    # \x1b or \033 followed by [ and then any number of digits, semicolons, or other chars, ending with a letter
    ansi_escape_pattern = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')
    return ansi_escape_pattern.sub('', text)


def create_log_handler(log_path, rotate_days=None):
    """
    Create log handler with exact Home Assistant rotation logic.

    This replicates the _create_log_file function from Home Assistant's bootstrap.py:
    - If log_rotate_days is set: Use TimedRotatingFileHandler (rotate at midnight)
    - If log_rotate_days is None: Use custom RotatingFileHandler with single backup on startup

    Args:
        log_path: Path to the log file
        rotate_days: Number of days to keep logs, or None for single backup

    Returns:
        File handler for logging
    """
    try:
        if rotate_days:
            # Rotate at midnight, keep rotate_days backups
            print(f"Using TimedRotatingFileHandler: rotate at midnight, keep {rotate_days} days")
            handler = TimedRotatingFileHandler(
                log_path,
                when="midnight",
                backupCount=rotate_days
            )
        else:
            # Single backup, rollover on startup (Home Assistant default behavior)
            print("Using RotatingFileHandler: single backup on startup")
            handler = RotatingFileHandlerWithoutShouldRollOver(
                log_path,
                backupCount=1
            )
            try:
                # Perform rollover on startup (just like HA did)
                handler.doRollover()
                print(f"Rolled over existing log to: {log_path}.1")
            except OSError as err:
                print(f"Error rolling over log file: {err}", file=sys.stderr)

        return handler
    except PermissionError as err:
        print(f"ERROR: Permission denied writing to {log_path}: {err}", file=sys.stderr)
        sys.exit(1)
    except OSError as err:
        print(f"ERROR: Cannot create log handler for {log_path}: {err}", file=sys.stderr)
        sys.exit(1)


def stream_logs_to_file(handler, verbose=False):
    """
    Stream logs from 'ha core logs --follow' to file using the handler.

    Args:
        handler: File handler to write logs to
        verbose: Whether to use verbose log format
    """
    # Create a logger and add the handler to enable rotation logic
    logger = logging.getLogger('ha_log_writer')
    logger.setLevel(logging.INFO)
    
    # Use custom formatter to output only the message (no timestamp, level, etc.)
    # This preserves the exact format from 'ha core logs'
    formatter = logging.Formatter('%(message)s')
    handler.setFormatter(formatter)
    
    logger.addHandler(handler)
    logger.propagate = False

    # Build ha command
    cmd = ["ha", "core", "logs", "--follow"]
    if verbose:
        cmd.append("--verbose")

    print(f"Starting log capture: {' '.join(cmd)}")

    # Start the process
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
        bufsize=1  # Line buffered
    )

    # Handle graceful shutdown
    def signal_handler(signum, frame):
        print("\nReceived shutdown signal, terminating...")
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        handler.close()
        sys.exit(0)

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    try:
        # Stream logs line by line
        for line in process.stdout:
            # Strip ANSI escape sequences (color codes) from the line
            clean_line = strip_ansi_codes(line.rstrip('\n'))
            # Use logger.info() which calls handler.emit()
            # This ensures rotation logic is properly triggered
            logger.info(clean_line)

    except KeyboardInterrupt:
        print("\nShutting down gracefully...")
    except Exception as e:
        print(f"Error streaming logs: {e}", file=sys.stderr)
        return 1
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        handler.close()

    # Don't treat SIGTERM/SIGINT as errors (negative return codes)
    if process.returncode and process.returncode > 0:
        print(f"ERROR: 'ha core logs' exited with code {process.returncode}", file=sys.stderr)
        return 1

    return 0


def main():
    """Main function."""
    print("=" * 60)
    print("Home Assistant Log File Writer")
    print("=" * 60)
    print(f"Log file: {LOG_FILE}")
    print(f"Rotation: {f'{LOG_ROTATE_DAYS} days' if LOG_ROTATE_DAYS else 'Single backup on startup (Home Assistant default)'}")
    print(f"Verbose: {VERBOSE}")
    print("=" * 60)

    # Ensure parent directory exists
    log_path = Path(LOG_FILE)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # Create the log handler with exact HA logic
    handler = create_log_handler(LOG_FILE, LOG_ROTATE_DAYS)

    # Check log file size and warn if too large
    if os.path.exists(LOG_FILE):
        try:
            size_mb = os.path.getsize(LOG_FILE) / (1024 * 1024)
            if size_mb > 100:
                print(f"WARNING: Log file is {size_mb:.1f}MB - consider enabling log rotation", file=sys.stderr)
        except OSError:
            pass  # Ignore if we can't check size

    # Stream logs to file
    return stream_logs_to_file(handler, VERBOSE)


if __name__ == "__main__":
    sys.exit(main())
