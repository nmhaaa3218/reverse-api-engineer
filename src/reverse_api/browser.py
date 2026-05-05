"""Browser management with Playwright for HAR recording."""

import io
import json
import random
import signal
import sys
from pathlib import Path

from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright
from playwright_stealth import Stealth
from rich.console import Console
from rich.status import Status

from .action_recorder import ActionRecorder, RecordedAction
from .utils import get_har_dir, get_timestamp

console = Console()

# Null stderr stream for suppressing logs
_null_stderr = io.StringIO()


def _null_logger(message: dict) -> None:
    """Null logger that discards all messages."""
    pass


# Realistic Chrome user agents (updated for late 2024/2025)
USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
]

# Stealth JavaScript to inject - bypasses common detection methods
STEALTH_JS = """
// Override navigator.webdriver
Object.defineProperty(navigator, 'webdriver', {
    get: () => undefined,
});

// Override navigator.plugins to look like a real browser
Object.defineProperty(navigator, 'plugins', {
    get: () => {
        const plugins = [
            { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
            { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
            { name: 'Native Client', filename: 'internal-nacl-plugin', description: '' },
        ];
        plugins.item = (index) => plugins[index];
        plugins.namedItem = (name) => plugins.find(p => p.name === name) || null;
        plugins.refresh = () => {};
        return plugins;
    },
});

// Override navigator.languages
Object.defineProperty(navigator, 'languages', {
    get: () => ['en-US', 'en'],
});

// Override navigator.permissions.query for notifications
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => {
    if (parameters.name === 'notifications') {
        return Promise.resolve({ state: Notification.permission });
    }
    return originalQuery(parameters);
};

// Remove automation-related properties from window
delete window.cdc_adoQpoasnfa76pfcZLmcfl_Array;
delete window.cdc_adoQpoasnfa76pfcZLmcfl_Object;
delete window.cdc_adoQpoasnfa76pfcZLmcfl_Promise;
delete window.cdc_adoQpoasnfa76pfcZLmcfl_Proxy;
delete window.cdc_adoQpoasnfa76pfcZLmcfl_Symbol;

// Override chrome runtime to look authentic
if (!window.chrome) {
    window.chrome = {};
}
window.chrome.runtime = {
    PlatformOs: { MAC: 'mac', WIN: 'win', ANDROID: 'android', CROS: 'cros', LINUX: 'linux', OPENBSD: 'openbsd' },
    PlatformArch: { ARM: 'arm', X86_32: 'x86-32', X86_64: 'x86-64' },
    PlatformNaclArch: { ARM: 'arm', X86_32: 'x86-32', X86_64: 'x86-64' },
    RequestUpdateCheckStatus: { THROTTLED: 'throttled', NO_UPDATE: 'no_update', UPDATE_AVAILABLE: 'update_available' },
    OnInstalledReason: { INSTALL: 'install', UPDATE: 'update', CHROME_UPDATE: 'chrome_update', SHARED_MODULE_UPDATE: 'shared_module_update' },
    OnRestartRequiredReason: { APP_UPDATE: 'app_update', OS_UPDATE: 'os_update', PERIODIC: 'periodic' },
};

// Fix iframe contentWindow detection
const originalAttachShadow = Element.prototype.attachShadow;
Element.prototype.attachShadow = function(init) {
    if (init && init.mode === 'closed') {
        init.mode = 'open';
    }
    return originalAttachShadow.call(this, init);
};

// Override WebGL vendor/renderer to look consistent
const getParameter = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function(parameter) {
    if (parameter === 37445) { // UNMASKED_VENDOR_WEBGL
        return 'Google Inc. (Apple)';
    }
    if (parameter === 37446) { // UNMASKED_RENDERER_WEBGL
        return 'ANGLE (Apple, ANGLE Metal Renderer: Apple M1 Pro, Unspecified Version)';
    }
    return getParameter.call(this, parameter);
};

// Do the same for WebGL2
const getParameter2 = WebGL2RenderingContext.prototype.getParameter;
WebGL2RenderingContext.prototype.getParameter = function(parameter) {
    if (parameter === 37445) {
        return 'Google Inc. (Apple)';
    }
    if (parameter === 37446) {
        return 'ANGLE (Apple, ANGLE Metal Renderer: Apple M1 Pro, Unspecified Version)';
    }
    return getParameter2.call(this, parameter);
};

// Override Permissions API
const originalPermissionsQuery = navigator.permissions.query;
navigator.permissions.query = function(permissionDesc) {
    if (permissionDesc.name === 'notifications') {
        return Promise.resolve({
            state: 'prompt',
            onchange: null
        });
    }
    return originalPermissionsQuery.call(navigator.permissions, permissionDesc);
};

// Spoof hardwareConcurrency to a realistic value
Object.defineProperty(navigator, 'hardwareConcurrency', {
    get: () => 8,
});

// Spoof deviceMemory
Object.defineProperty(navigator, 'deviceMemory', {
    get: () => 8,
});

// Spoof connection info
if (navigator.connection) {
    Object.defineProperty(navigator.connection, 'rtt', {
        get: () => 50,
    });
}

// Hide automation in chrome.app
if (window.chrome && window.chrome.app) {
    window.chrome.app.isInstalled = false;
    window.chrome.app.InstallState = { DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' };
    window.chrome.app.RunningState = { CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' };
}

console.log('Stealth mode activated');
"""


# Default Chrome profile path on macOS
CHROME_USER_DATA_DIR = Path.home() / "Library/Application Support/Google/Chrome"


def get_chrome_profile_dir() -> Path | None:
    """Get Chrome user data directory if it exists."""
    if CHROME_USER_DATA_DIR.exists():
        return CHROME_USER_DATA_DIR
    return None


class ManualBrowser:
    """Manages a Playwright browser session with HAR recording.

    Supports two modes:
    - Real Chrome: Uses your actual Chrome browser with existing profile (best for stealth)
    - Stealth Chromium: Falls back to Playwright's Chromium with stealth patches
    """

    def __init__(
        self,
        run_id: str,
        prompt: str,
        output_dir: str | None = None,
        use_real_chrome: bool = True,  # New option to use real Chrome
        enable_action_recording: bool = False,
    ):
        self.run_id = run_id
        self.prompt = prompt
        self.output_dir = output_dir
        self.use_real_chrome = use_real_chrome
        self.enable_action_recording = enable_action_recording

        self.har_dir = get_har_dir(run_id, output_dir)
        self.har_path = self.har_dir / "recording.har"
        self.metadata_path = self.har_dir / "metadata.json"

        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._start_time: str | None = None
        self._user_agent = random.choice(USER_AGENTS)
        self._using_persistent = False  # Track if using persistent context

        self.action_recorder = ActionRecorder() if enable_action_recording else None

    def _inject_action_recorder(self, page: Page) -> None:
        """Inject action recording script into page.

        Uses console.log + page.on('console') for reliable capture.
        Works best with stealth Chromium mode (not real Chrome).
        """
        if not self.enable_action_recording:
            return

        # Simple JS that logs actions to console with a special prefix
        recorder_js = """
        window.__recordedActions = [];
        window.__lastUrl = null;
        
        document.addEventListener('click', (e) => {
            const el = e.target;
            
            // Build a robust selector by traversing up to find parent with ID
            function buildSelector(element) {
                // Priority 1: data-testid on element or close parent
                let current = element;
                for (let i = 0; i < 3 && current; i++) {
                    if (current.dataset && current.dataset.testid) {
                        const path = i === 0 ? '' : ' ' + getPathFromAncestor(current, element);
                        return '[data-testid="' + current.dataset.testid.replace(/"/g, '\\"') + '"]' + path;
                    }
                    current = current.parentElement;
                }
                
                // Priority 2: element has short ID
                if (element.id && element.id.length < 20) {
                    return '#' + element.id;
                }
                
                // Priority 3: find parent with ID and build path
                current = element.parentElement;
                let depth = 1;
                while (current && depth < 5) {
                    if (current.id && current.id.length < 20) {
                        const path = getPathFromAncestor(current, element);
                        const selector = '#' + current.id + ' > ' + path;
                        // Verify it's unique
                        if (document.querySelectorAll(selector).length === 1) {
                            return selector;
                        }
                    }
                    current = current.parentElement;
                    depth++;
                }
                
                // Priority 4: name attribute
                if (element.name) {
                    return '[name="' + element.name.replace(/"/g, '\\"') + '"]';
                }
                
                // Priority 5: aria-label
                if (element.getAttribute && element.getAttribute('aria-label')) {
                    return '[aria-label="' + element.getAttribute('aria-label').replace(/"/g, '\\"') + '"]';
                }
                
                // Priority 6: role + text for buttons
                if ((element.tagName === 'BUTTON' || element.role === 'button') && element.innerText) {
                    const text = element.innerText.trim().substring(0, 30);
                    if (text && !text.includes('\\n')) {
                        return 'button:has-text(' + JSON.stringify(text) + ')';
                    }
                }
                
                // Priority 7: link text
                if (element.tagName === 'A' && element.innerText) {
                    const text = element.innerText.trim().substring(0, 30);
                    if (text && !text.includes('\\n')) {
                        return 'a:has-text(' + JSON.stringify(text) + ')';
                    }
                }
                
                // Priority 8: tag + class
                if (element.className && typeof element.className === 'string') {
                    const cls = element.className.split(' ').filter(c => c && c.length < 30 && !c.includes('hover') && !c.includes('active'))[0];
                    if (cls) {
                        const baseSelector = element.tagName.toLowerCase() + '.' + cls;
                        const matches = document.querySelectorAll(baseSelector);
                        if (matches.length === 1) return baseSelector;
                        const idx = Array.from(matches).indexOf(element);
                        if (idx >= 0) return baseSelector + ' >> nth=' + idx;
                    }
                }
                
                // Fallback: tag name
                return element.tagName.toLowerCase();
            }
            
            // Get path from ancestor to descendant
            function getPathFromAncestor(ancestor, descendant) {
                if (ancestor === descendant) return descendant.tagName.toLowerCase();
                
                let path = [];
                let current = descendant;
                while (current && current !== ancestor) {
                    path.unshift(current.tagName.toLowerCase());
                    current = current.parentElement;
                }
                return path.join(' > ');
            }
            
            const selector = buildSelector(el);
            const action = {type: 'click', selector: selector, timestamp: Date.now()};
            window.__recordedActions.push(action);
            console.log('__ACTION__' + JSON.stringify(action));
        }, true);
        
        document.addEventListener('input', (e) => {
            if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') {
                const el = e.target;
                let selector = '';
                if (el.id && el.id.length < 20) selector = '#' + el.id;
                else if (el.name) selector = '[name="' + el.name.replace(/"/g, '\\"') + '"]';
                else if (el.placeholder) selector = '[placeholder="' + el.placeholder.replace(/"/g, '\\"') + '"]';
                else selector = el.tagName.toLowerCase();
                
                const action = {type: 'fill', selector: selector, value: el.value, timestamp: Date.now()};
                window.__recordedActions.push(action);
                console.log('__ACTION__' + JSON.stringify(action));
            }
        }, true);
        
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                const el = e.target;
                let selector = '';
                if (el.id && el.id.length < 20) selector = '#' + el.id;
                else if (el.name) selector = '[name="' + el.name.replace(/"/g, '\\"') + '"]';
                else selector = el.tagName.toLowerCase();
                
                const action = {type: 'press', selector: selector, value: 'Enter', timestamp: Date.now()};
                window.__recordedActions.push(action);
                console.log('__ACTION__' + JSON.stringify(action));
            }
        }, true);
        
        // Only log navigation for top-level main frame, not iframes
        if (window === window.top) {
            const url = window.location.href;
            // Skip about:blank, blob:, data:, service workers, embeds
            if (url && !url.startsWith('about:') && !url.startsWith('blob:') && 
                !url.startsWith('data:') && !url.includes('/embed') && 
                !url.includes('service_worker') && !url.includes('googletagmanager')) {
                // Only log if URL changed
                if (url !== window.__lastUrl) {
                    window.__lastUrl = url;
                    console.log('__ACTION__' + JSON.stringify({type: 'navigate', url: url, timestamp: Date.now()}));
                }
            }
        }
        """

        # Listen to console for actions
        import json

        last_url = [None]  # Mutable to track last URL

        def on_console(msg):
            text = msg.text
            if text.startswith("__ACTION__"):
                try:
                    action_json = text[10:]  # Remove '__ACTION__' prefix
                    action_data = json.loads(action_json)

                    # Filter duplicate navigations
                    if action_data.get("type") == "navigate":
                        url = action_data.get("url", "")
                        if url == last_url[0]:
                            return  # Skip duplicate
                        last_url[0] = url

                    if self.action_recorder:
                        self.action_recorder.add_action(RecordedAction(**action_data))
                except Exception as e:
                    console.print(f" [dim]action parse error: {e}[/dim]")

        page.on("console", on_console)
        page.add_init_script(recorder_js)

        console.print(" [dim]action recording enabled[/dim]")

    def _save_metadata(self, end_time: str) -> None:
        """Save run metadata to JSON file."""
        metadata = {
            "run_id": self.run_id,
            "prompt": self.prompt,
            "start_time": self._start_time,
            "end_time": end_time,
            "har_file": str(self.har_path),
        }
        with open(self.metadata_path, "w") as f:
            json.dump(metadata, f, indent=2)

    def _handle_signal(self, signum, frame) -> None:
        """Handle interrupt signals gracefully."""
        console.print("\n\n [dim]terminating capture...[/dim]")
        self.close()
        sys.exit(0)

    def _inject_stealth(self, page: Page) -> None:
        """Inject stealth scripts into page before any other scripts run."""
        page.add_init_script(STEALTH_JS)

    def _start_with_real_chrome(self, start_url: str | None = None) -> Path:
        """Start using the real Chrome browser with user's profile."""
        import shutil
        import tempfile

        chrome_profile = get_chrome_profile_dir()
        if not chrome_profile:
            console.print(" [yellow]chrome profile not found, falling back to stealth mode[/yellow]")
            return self._start_with_stealth_chromium(start_url)

        # Create a temporary profile directory
        temp_profile_dir = Path(tempfile.mkdtemp(prefix="chrome_profile_"))

        console.print(" [dim]using real chrome (profile copy)[/dim]")
        console.print(" [yellow]⚠️  please browse in the FIRST tab only[/yellow]")
        console.print(" [yellow]    (new tabs may not be recorded)[/yellow]")
        console.print()

        try:
            # Use launch_persistent_context with channel="chrome" to use real Chrome binary
            self._context = self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(temp_profile_dir),
                channel="chrome",  # Use real Chrome binary
                headless=False,
                record_har_path=str(self.har_path),
                record_har_content="embed",
                no_viewport=True,
                args=[
                    "--start-maximized",
                    "--disable-blink-features=AutomationControlled",
                ],
                ignore_default_args=["--enable-automation", "--no-sandbox"],
            )
            self._using_persistent = True

            for existing_page in self._context.pages:
                try:
                    existing_page.close()
                except Exception:
                    pass

            # For HAR recording & context
            page = self._context.new_page()

            if self.enable_action_recording:
                self._inject_action_recorder(page)

            if start_url:
                page.goto(start_url, wait_until="domcontentloaded")
            else:
                page.goto("https://www.google.com", wait_until="domcontentloaded")

            # Wait for browser to close
            try:
                while self._context.pages:
                    self._context.pages[0].wait_for_timeout(100)
            except Exception:
                pass

            return self.close()

        finally:
            # Clean up temp profile
            try:
                shutil.rmtree(temp_profile_dir, ignore_errors=True)
            except Exception:
                pass

    def _start_with_stealth_chromium(self, start_url: str | None = None) -> Path:
        """Start using Playwright's Chromium with stealth patches."""
        # Comprehensive stealth Chrome arguments
        chrome_args = [
            "--start-maximized",
            "--disable-blink-features=AutomationControlled",
            "--disable-features=IsolateOrigins,site-per-process",
            "--disable-infobars",
            "--disable-dev-shm-usage",
            "--disable-background-networking",
            "--disable-default-apps",
            "--disable-extensions",
            "--disable-sync",
            "--disable-translate",
            "--no-first-run",
            "--no-default-browser-check",
            "--no-service-autorun",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--disable-background-timer-throttling",
            "--disable-ipc-flooding-protection",
            "--disable-hang-monitor",
            "--disable-prompt-on-repost",
            "--disable-client-side-phishing-detection",
            "--disable-webrtc-hw-encoding",
            "--disable-webrtc-hw-decoding",
            "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
            "--enable-features=NetworkService,NetworkServiceInProcess",
            "--disable-component-update",
            "--disable-domain-reliability",
            "--disable-features=AutofillServerCommunication",
            "--password-store=basic",
            "--use-mock-keychain",
        ]

        self._browser = self._playwright.chromium.launch(
            headless=False,
            args=chrome_args,
            ignore_default_args=["--enable-automation", "--no-sandbox"],
        )

        # Create context with HAR recording and realistic settings
        self._context = self._browser.new_context(
            record_har_path=str(self.har_path),
            record_har_content="embed",
            no_viewport=True,
            locale="en-US",
            timezone_id="America/New_York",
            user_agent=self._user_agent,
            screen={"width": 1920, "height": 1080},
            color_scheme="light",
            reduced_motion="no-preference",
            forced_colors="none",
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "sec-ch-ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"macOS"',
            },
        )

        # Apply playwright-stealth evasions
        stealth = Stealth()
        stealth.apply_stealth_sync(self._context)

        # Add custom stealth init script
        self._context.add_init_script(STEALTH_JS)

        # Open initial page
        page = self._context.new_page()

        if self.enable_action_recording:
            self._inject_action_recorder(page)

        if start_url:
            page.goto(start_url, wait_until="domcontentloaded")
        else:
            # For HAR recording & context
            page.goto("about:blank")

        # Wait for browser to close
        try:
            while self._context.pages:
                self._context.pages[0].wait_for_timeout(100)
        except Exception:
            pass

        return self.close()

    def start(self, start_url: str | None = None) -> Path:
        """Start the browser with HAR recording enabled. Returns HAR path when done."""
        self._start_time = get_timestamp()

        # Set up signal handlers for graceful shutdown
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

        console.print(" [dim]capture starting...[/dim]")
        console.print(f" [dim]━[/dim] [white]{self.run_id}[/white]")
        console.print(f" [dim]goal[/dim]  [white]{self.prompt}[/white]")
        console.print()
        console.print(" [dim]navigate and interact to record traffic[/dim]")
        console.print(" [dim]close browser or ctrl+c to finalize[/dim]")
        console.print()

        self._playwright = sync_playwright().start()

        # Try real Chrome first (better for avoiding detection)
        # Fall back to stealth Chromium if Chrome not available
        if self.use_real_chrome:
            return self._start_with_real_chrome(start_url)
        else:
            return self._start_with_stealth_chromium(start_url)

    def close(self) -> Path:
        """Close the browser and save HAR file. Returns HAR path."""
        end_time = get_timestamp()

        console.print(" [dim]browser closed[/dim]")

        if self._context:
            with Status(
                " [dim]handling har... can take a bit[/dim]",
                console=console,
                spinner="dots",
            ) as status:
                try:
                    status.update(" [dim]flushing network traffic...[/dim]")
                    import time

                    time.sleep(1)

                    status.update(" [dim]saving har file...[/dim]")
                    self._context.close()

                    if self.har_path.exists():
                        har_size = self.har_path.stat().st_size
                        status.update(f" [dim]har saved: {har_size:,} bytes[/dim]")
                    else:
                        console.print(" [yellow]warning: har file was not created[/yellow]")

                except Exception as e:
                    console.print(f" [yellow]warning: error saving har: {e}[/yellow]")
                    if self.har_path.exists():
                        console.print(" [dim]har file exists despite error[/dim]")
                self._context = None

        # Only close browser if not using persistent context
        if self._browser and not self._using_persistent:
            try:
                self._browser.close()
            except Exception:
                pass
            self._browser = None

        if self._playwright:
            try:
                self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

        # Save metadata
        self._save_metadata(end_time)

        console.print(" [dim]capture saved[/dim]")
        console.print(" [dim]metadata synced[/dim]")

        if self.action_recorder:
            try:
                actions_path = self.har_dir / "actions.json"
                self.action_recorder.save(actions_path)
                console.print(" [dim]actions saved[/dim]")
            except Exception as e:
                console.print(f" [yellow]warning: error saving actions: {e}[/yellow]")

        return self.har_path

