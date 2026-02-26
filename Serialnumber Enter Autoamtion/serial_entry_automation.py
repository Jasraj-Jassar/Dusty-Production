import argparse
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

try:
    from selenium import webdriver
    from selenium.common.exceptions import (
        ElementClickInterceptedException,
        ElementNotInteractableException,
        InvalidElementStateException,
        NoSuchElementException,
        StaleElementReferenceException,
        TimeoutException,
        WebDriverException,
    )
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.remote.webdriver import WebDriver
    from selenium.webdriver.remote.webelement import WebElement
except ImportError as exc:
    raise SystemExit(
        "Missing dependency: selenium. Run DustyBot.cmd or: pip install -r ..\\requirements.txt"
    ) from exc

try:
    from pynput import keyboard
except Exception:
    keyboard = None


DEFAULT_SERIAL_INPUT_ID = "SelectSerial_tbxSerialNumber"
DEFAULT_CREATE_BUTTON_XPATH = (
    "//button[contains(@class,'ep-button') and contains(normalize-space(.), 'Create')]"
)
DEFAULT_TIMEOUT_SECONDS = 15.0
DEFAULT_POST_CLICK_DELAY_SECONDS = 1.0
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_DEBUGGER_ADDRESS = "127.0.0.1:9222"
DEFAULT_PROFILE_DIR = str((Path(__file__).resolve().parent / ".chrome_profile").resolve())
DEFAULT_FALLBACK_PROFILE_DIR = str(
    (Path(__file__).resolve().parent / ".chrome_profile_debug").resolve()
)
DEFAULT_DEBUGGER_SCAN_PORTS = [9222, 9223, 9224, 9225, 9333]
DEFAULT_PAGE_READY_TIMEOUT_SECONDS = 90.0

STOP_EVENT = threading.Event()
SERIAL_SUFFIX_PATTERN = re.compile(r"^(?P<prefix>.*?)(?P<number>\d+)$")


@dataclass
class RunConfig:
    total: int
    url: str
    serial_prefix: str
    serial_start_value: int
    serial_width: int
    serial_input_id: str = DEFAULT_SERIAL_INPUT_ID
    create_button_xpath: str = DEFAULT_CREATE_BUTTON_XPATH
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    post_click_delay_seconds: float = DEFAULT_POST_CLICK_DELAY_SECONDS
    max_attempts_per_serial: int = DEFAULT_MAX_ATTEMPTS
    chrome_profile_dir: str = DEFAULT_PROFILE_DIR


def ask_non_empty(prompt: str, default: Optional[str] = None) -> str:
    while True:
        value = input(prompt).strip()
        if value:
            return value
        if default is not None:
            return default
        print("Input is required.")


def ask_positive_int(prompt: str, default: Optional[int] = None) -> int:
    while True:
        raw = input(prompt).strip()
        if not raw and default is not None:
            return default
        try:
            value = int(raw)
            if value > 0:
                return value
        except ValueError:
            pass
        print("Enter a positive whole number.")


def parse_serial_seed(seed: str) -> Tuple[str, int, int]:
    match = SERIAL_SUFFIX_PATTERN.match(seed.strip())
    if not match:
        raise ValueError("Base serial must end with at least one digit.")
    prefix = match.group("prefix")
    number_text = match.group("number")
    return prefix, int(number_text), len(number_text)


def format_serial(prefix: str, start_value: int, width: int, offset: int) -> str:
    return f"{prefix}{start_value + offset:0{width}d}"


def on_press(key) -> Optional[bool]:
    try:
        if key.char and key.char.lower() == "q":
            STOP_EVENT.set()
            print("\nStop key detected ('q'). Finishing current attempt and exiting.")
            return False
    except AttributeError:
        if key == keyboard.Key.esc:
            STOP_EVENT.set()
            print("\nStop key detected (Esc). Finishing current attempt and exiting.")
            return False
    return None


def start_stop_listener():
    if keyboard is None:
        print("pynput not available, stop hotkey disabled. Use Ctrl+C to stop.")
        return None
    listener = keyboard.Listener(on_press=on_press)
    listener.start()
    return listener


def safe_click(driver: WebDriver, element: WebElement) -> None:
    try:
        element.click()
        return
    except ElementClickInterceptedException:
        pass

    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
    time.sleep(0.15)
    try:
        element.click()
    except ElementClickInterceptedException:
        driver.execute_script("arguments[0].click();", element)


def find_visible_element(driver: WebDriver, by: str, value: str) -> WebElement:
    elements = driver.find_elements(by, value)
    if not elements:
        raise NoSuchElementException(f"No element found by {by}={value}")
    for element in elements:
        if element.is_displayed():
            return element
    return elements[0]


def find_element_in_default_or_iframes(
    driver: WebDriver, by: str, value: str, timeout_seconds: float
) -> WebElement:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        driver.switch_to.default_content()
        elements = driver.find_elements(by, value)
        if elements:
            return elements[0]

        frames = driver.find_elements(By.CSS_SELECTOR, "iframe, frame")
        for idx in range(len(frames)):
            driver.switch_to.default_content()
            refreshed_frames = driver.find_elements(By.CSS_SELECTOR, "iframe, frame")
            if idx >= len(refreshed_frames):
                continue

            driver.switch_to.frame(refreshed_frames[idx])
            elements = driver.find_elements(by, value)
            if elements:
                return elements[0]

        time.sleep(0.2)

    raise TimeoutException(f"Element not found by {by}={value} in default context or iframes.")


def get_serial_input_element(driver: WebDriver, config: RunConfig) -> WebElement:
    host = find_element_in_default_or_iframes(
        driver,
        By.ID,
        config.serial_input_id,
        config.timeout_seconds,
    )

    tag = (host.tag_name or "").lower()
    if tag in {"input", "textarea"}:
        return host

    child_inputs = host.find_elements(By.CSS_SELECTOR, "input:not([type='hidden']), textarea")
    for child in child_inputs:
        if child.is_displayed() and child.is_enabled():
            return child
    if child_inputs:
        return child_inputs[0]

    serial_candidates = driver.find_elements(
        By.XPATH,
        (
            "//input[not(@type='hidden') and "
            "(contains(@id,'Serial') or contains(@name,'Serial'))]"
            "|//textarea[contains(@id,'Serial') or contains(@name,'Serial')]"
        ),
    )
    for candidate in serial_candidates:
        if candidate.is_displayed() and candidate.is_enabled():
            return candidate
    if serial_candidates:
        return serial_candidates[0]

    raise NoSuchElementException(
        f"Could not resolve a typeable input from element id '{config.serial_input_id}'."
    )


def js_set_value(driver: WebDriver, element: WebElement, value: str) -> None:
    driver.execute_script(
        """
        const el = arguments[0];
        const value = arguments[1];
        if (!el) {
            return;
        }
        el.focus();
        const prototype = Object.getPrototypeOf(el);
        const descriptor = prototype ? Object.getOwnPropertyDescriptor(prototype, 'value') : null;
        if (descriptor && descriptor.set) {
            descriptor.set.call(el, value);
        } else {
            el.value = value;
        }
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
        el.dispatchEvent(new Event('blur', { bubbles: true }));
        """,
        element,
        value,
    )


def enter_serial_value(driver: WebDriver, serial_input: WebElement, serial: str) -> None:
    try:
        serial_input.click()
        serial_input.send_keys(Keys.CONTROL, "a")
        serial_input.send_keys(Keys.DELETE)
        serial_input.send_keys(serial)
        return
    except (ElementNotInteractableException, InvalidElementStateException):
        pass

    js_set_value(driver, serial_input, serial)
    current_value = serial_input.get_attribute("value") or ""
    if current_value.strip() != serial:
        raise ElementNotInteractableException(
            f"Unable to set serial in input. Current value is '{current_value}'."
        )


def get_create_button(driver: WebDriver, config: RunConfig) -> WebElement:
    deadline = time.time() + config.timeout_seconds
    fallback_xpath = (
        "//button[contains(@class,'ep-button') and contains(normalize-space(.), 'Create')]"
    )

    last_error: Optional[Exception] = None
    while time.time() < deadline:
        try:
            button = find_visible_element(driver, By.XPATH, config.create_button_xpath)
            if button.is_enabled():
                return button
        except NoSuchElementException as exc:
            last_error = exc

        try:
            fallback = find_visible_element(driver, By.XPATH, fallback_xpath)
            if fallback.is_enabled():
                return fallback
        except NoSuchElementException as exc:
            last_error = exc

        time.sleep(0.2)

    if last_error is not None:
        raise TimeoutException(f"Create button not available: {last_error}")
    raise TimeoutException("Create button not available before timeout.")


def fill_and_submit_serial(driver: WebDriver, config: RunConfig, serial: str) -> None:
    serial_input = get_serial_input_element(driver, config)
    enter_serial_value(driver, serial_input, serial)

    create_button = get_create_button(driver, config)
    safe_click(driver, create_button)
    time.sleep(config.post_click_delay_seconds)


def build_config_from_values(base_serial: str, total: int, url: str) -> RunConfig:
    serial_prefix, serial_start_value, serial_width = parse_serial_seed(base_serial)
    if total <= 0:
        raise ValueError("Total number to enter must be a positive whole number.")
    if not url.strip():
        raise ValueError("Epicor URL is required.")
    return RunConfig(
        total=total,
        url=url.strip(),
        serial_prefix=serial_prefix,
        serial_start_value=serial_start_value,
        serial_width=serial_width,
    )


def build_config_interactive() -> RunConfig:
    while True:
        base_serial = ask_non_empty(
            "Enter initial serial number (example: AFPA-100-030): "
        )
        try:
            parse_serial_seed(base_serial)
            break
        except ValueError as exc:
            print(exc)

    total = ask_positive_int("Enter total number to enter: ")
    url = ask_non_empty("Enter the full Epicor serial entry page URL: ")
    return build_config_from_values(base_serial, total, url)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Automate Epicor serial entry creation."
    )
    parser.add_argument("--serial", help="Starting serial number, e.g. AFPA-100-030")
    parser.add_argument(
        "--count",
        type=int,
        help="How many sequential serial numbers to create",
    )
    parser.add_argument("--url", help="Full Epicor serial-entry page URL")
    return parser.parse_args()


def normalize_debugger_address(address: str) -> str:
    clean = address.strip().rstrip("/")
    clean = clean.replace("http://", "").replace("https://", "")
    return clean


def debugger_is_available(debugger_address: str, timeout_seconds: float = 1.5) -> bool:
    endpoint = f"http://{debugger_address}/json/version"
    try:
        with urlopen(endpoint, timeout=timeout_seconds) as response:
            return 200 <= response.status < 300
    except (URLError, HTTPError, ValueError):
        return False


def find_chrome_executable() -> Optional[str]:
    candidates = [
        Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
        Path("C:/Program Files (x86)/Google/Chrome/Application/chrome.exe"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)

    for command_name in ("chrome", "chrome.exe"):
        resolved = shutil.which(command_name)
        if resolved:
            return resolved
    return None


def launch_debug_chrome(debugger_address: str, profile_dir: str) -> bool:
    chrome_exe = find_chrome_executable()
    if not chrome_exe:
        print("Chrome executable not found on system PATH or default install paths.")
        return False

    address = normalize_debugger_address(debugger_address)
    try:
        _, port_str = address.rsplit(":", 1)
        port = int(port_str)
    except ValueError:
        print(f"Invalid debugger address: {debugger_address}")
        return False

    Path(profile_dir).mkdir(parents=True, exist_ok=True)
    launch_command = [
        chrome_exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--new-window",
        "about:blank",
    ]

    try:
        subprocess.Popen(
            launch_command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        print(f"Failed to launch Chrome in debug mode: {exc.__class__.__name__}: {exc}")
        return False

    deadline = time.time() + 12.0
    while time.time() < deadline:
        if debugger_is_available(address):
            return True
        time.sleep(0.35)

    return False


def discover_debugger_address(preferred: Optional[str]) -> Optional[str]:
    candidates: List[str] = []
    if preferred:
        candidates.append(normalize_debugger_address(preferred))
    for port in DEFAULT_DEBUGGER_SCAN_PORTS:
        candidate = f"127.0.0.1:{port}"
        if candidate not in candidates:
            candidates.append(candidate)
    for candidate in candidates:
        if debugger_is_available(candidate):
            return candidate
    return None


def setup_driver(config: RunConfig) -> Tuple[WebDriver, bool]:
    requested = normalize_debugger_address(DEFAULT_DEBUGGER_ADDRESS)
    address = discover_debugger_address(requested)

    if address:
        if address != requested:
            print(
                f"Debugger not found at {requested}; attaching to detected Chrome at {address}."
            )
        else:
            print(f"Attaching to existing Chrome at {address}...")
        attach_options = Options()
        attach_options.add_experimental_option("debuggerAddress", address)
        try:
            return webdriver.Chrome(options=attach_options), True
        except WebDriverException as exc:
            print(
                f"Attach failed ({exc.__class__.__name__}). "
                "Trying to start a debuggable Chrome session."
            )

    print(
        f"No debuggable Chrome found (checked {requested} and common localhost debug ports). "
        "Starting Chrome in debug mode now."
    )

    primary_profile = config.chrome_profile_dir
    print(f"Trying profile: {primary_profile}")
    launched = launch_debug_chrome(requested, primary_profile)
    if launched:
        time.sleep(0.5)
        address = discover_debugger_address(requested)
        if address:
            attach_options = Options()
            attach_options.add_experimental_option("debuggerAddress", address)
            return webdriver.Chrome(options=attach_options), False

    print(
        "Primary profile could not start in debug mode. "
        "Trying dedicated fallback profile once."
    )
    fallback_profile = DEFAULT_FALLBACK_PROFILE_DIR
    print(f"Trying profile: {fallback_profile}")
    launched_fallback = launch_debug_chrome(requested, fallback_profile)
    if launched_fallback:
        time.sleep(0.5)
        address = discover_debugger_address(requested)
        if address:
            attach_options = Options()
            attach_options.add_experimental_option("debuggerAddress", address)
            print(
                "Using fallback profile. If Epicor asks you to log in once, "
                "future runs will stay logged in."
            )
            return webdriver.Chrome(options=attach_options), False

    raise RuntimeError(
        "Could not attach or start a debuggable Chrome session. "
        "Close all Chrome windows and rerun once."
    )


def wait_for_page_ready(driver: WebDriver, config: RunConfig) -> None:
    deadline = time.time() + DEFAULT_PAGE_READY_TIMEOUT_SECONDS
    last_error: Optional[Exception] = None

    while time.time() < deadline and not STOP_EVENT.is_set():
        try:
            get_serial_input_element(driver, config)
            return
        except (TimeoutException, NoSuchElementException, WebDriverException) as exc:
            last_error = exc
            time.sleep(1.0)

    if last_error is not None:
        raise TimeoutException(
            "Serial input was not ready in time. "
            f"Last error: {last_error.__class__.__name__}: {last_error}"
        )
    raise TimeoutException("Serial input was not ready in time.")


def run_automation(driver: WebDriver, config: RunConfig) -> Tuple[int, List[str]]:
    success_count = 0
    failed_serials: List[str] = []

    for index in range(config.total):
        if STOP_EVENT.is_set():
            break

        serial = format_serial(
            config.serial_prefix,
            config.serial_start_value,
            config.serial_width,
            index,
        )

        serial_success = False
        for attempt in range(1, config.max_attempts_per_serial + 1):
            if STOP_EVENT.is_set():
                break

            try:
                fill_and_submit_serial(driver, config, serial)
                serial_success = True
                success_count += 1
                print(f"[{index + 1}/{config.total}] Entered: {serial}")
                break
            except (
                TimeoutException,
                StaleElementReferenceException,
                ElementClickInterceptedException,
                WebDriverException,
            ) as exc:
                print(
                    f"[{index + 1}/{config.total}] Attempt {attempt}/{config.max_attempts_per_serial} "
                    f"failed for {serial}: {exc.__class__.__name__}: {exc}"
                )
                if attempt < config.max_attempts_per_serial:
                    time.sleep(0.75)

        if not serial_success:
            failed_serials.append(serial)

    return success_count, failed_serials


def main() -> None:
    listener = None
    driver = None

    try:
        args = parse_args()
        has_any_cli_input = any([args.serial is not None, args.count is not None, args.url is not None])
        if has_any_cli_input:
            if not (args.serial and args.count is not None and args.url):
                raise SystemExit(
                    "When using CLI mode, provide --serial, --count, and --url together."
                )
            try:
                config = build_config_from_values(args.serial, args.count, args.url)
            except ValueError as exc:
                raise SystemExit(f"Invalid CLI input: {exc}") from exc
        else:
            config = build_config_interactive()

        print("Press 'q' or Esc at any time to stop gracefully.")
        listener = start_stop_listener()

        driver, attached_to_existing = setup_driver(config)
        if attached_to_existing:
            driver.switch_to.new_window("tab")
            print("Opened a new tab in your existing Chrome session.")

        driver.get(config.url)
        print("Waiting for Epicor serial entry page to be ready...")
        wait_for_page_ready(driver, config)

        success_count, failed_serials = run_automation(driver, config)
        stopped_early = STOP_EVENT.is_set()

        print("\nRun complete.")
        print(f"Requested: {config.total}")
        print(f"Succeeded: {success_count}")
        print(f"Failed: {len(failed_serials)}")
        if stopped_early:
            print("Status: Stopped early by user.")
        else:
            print("Status: Finished requested batch.")

        if failed_serials:
            print("Failed serials:")
            for serial in failed_serials:
                print(f"- {serial}")

    except KeyboardInterrupt:
        print("\nStopped by Ctrl+C.")
    except Exception as exc:
        print(f"\nFatal error: {exc.__class__.__name__}: {exc}")
    finally:
        if listener is not None:
            listener.stop()
        # Browser stays open because Chrome is launched with detach=True.


if __name__ == "__main__":
    main()
