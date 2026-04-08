"""Desktop control — pure vision. Screenshots + mouse/keyboard simulation.

No OS APIs. No accessibility trees. No DOM inspection.
Just eyes (screenshots) and hands (mouse + keyboard).
Like a human sitting at a computer.

Auto-detects WSL2 and uses Windows PowerShell to control the real desktop.
On native Linux, uses scrot + xdotool.
"""

from __future__ import annotations

import asyncio
import io
import os
import shutil
import sys
import tempfile

from PIL import Image


def _is_wsl2() -> bool:
    """Detect if running inside WSL2."""
    try:
        with open("/proc/version") as f:
            return "microsoft" in f.read().lower()
    except OSError:
        return False


# ---------------------------------------------------------------------------
# PowerShell init script — loaded once into a persistent process.
# Defines Win32 functions and action commands.
# ---------------------------------------------------------------------------
_PS_INIT = r"""
$ErrorActionPreference = 'Stop'

# Win32 input functions — the "hands"
$sig = @'
[DllImport("user32.dll")] public static extern bool SetCursorPos(int X, int Y);
[DllImport("user32.dll")] public static extern void mouse_event(uint dwFlags, int dx, int dy, int dwData, IntPtr dwExtraInfo);
[DllImport("user32.dll")] public static extern void keybd_event(byte bVk, byte bScan, uint dwFlags, IntPtr dwExtraInfo);
[DllImport("user32.dll")] public static extern short VkKeyScan(char ch);
[DllImport("kernel32.dll")] public static extern IntPtr GetConsoleWindow();
[DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
'@
try { $W = Add-Type -MemberDefinition $sig -Name W32 -Namespace VP -PassThru }
catch { $W = [VP.W32] }

# Hide console window — we communicate via stdin/stdout only
$hwnd = $W::GetConsoleWindow()
if ($hwnd -ne [IntPtr]::Zero) { $W::ShowWindow($hwnd, 0) | Out-Null }

# Screenshot dependencies
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$KU = [uint32]0x0002  # KEYEVENTF_KEYUP
$KE = [uint32]0x0001  # KEYEVENTF_EXTENDEDKEY

$extKeys = @(0x5B,0x5C,0x25,0x26,0x27,0x28,0x2D,0x2E,0x21,0x22,0x23,0x24)

function Get-KF($vk, $up) {
    $f = [uint32]0
    if ($extKeys -contains $vk) { $f = $f -bor $KE }
    if ($up) { $f = $f -bor $KU }
    return $f
}

$modOnly = @{ 'super'=0x5B; 'ctrl'=0x11; 'alt'=0x12; 'shift'=0x10 }
$vkMap = @{
    'Return'=0x0D; 'Enter'=0x0D; 'Escape'=0x1B; 'Tab'=0x09
    'BackSpace'=0x08; 'Delete'=0x2E; 'space'=0x20; 'Insert'=0x2D
    'Up'=0x26; 'Down'=0x28; 'Left'=0x25; 'Right'=0x27
    'Home'=0x24; 'End'=0x23; 'Page_Up'=0x21; 'Page_Down'=0x22
    'F1'=0x70;'F2'=0x71;'F3'=0x72;'F4'=0x73;'F5'=0x74;'F6'=0x75
    'F7'=0x76;'F8'=0x77;'F9'=0x78;'F10'=0x79;'F11'=0x7A;'F12'=0x7B
}
$modVk = @{ 'ctrl'=0x11; 'alt'=0x12; 'shift'=0x10; 'super'=0x5B }

function Do-Key($combo) {
    $parts = $combo -split '\+'
    $key = $parts[-1]
    $mods = @()
    for ($i = 0; $i -lt $parts.Length - 1; $i++) { $mods += $parts[$i].ToLower() }
    if ($parts.Length -eq 1 -and $modOnly.ContainsKey($parts[0].ToLower())) {
        $vk = $modOnly[$parts[0].ToLower()]
        $W::keybd_event([byte]$vk, 0, (Get-KF $vk $false), [IntPtr]::Zero)
        Start-Sleep -Milliseconds 100
        $W::keybd_event([byte]$vk, 0, (Get-KF $vk $true), [IntPtr]::Zero)
        return
    }
    if ($vkMap.ContainsKey($key)) { $vk = $vkMap[$key] }
    elseif ($key.Length -eq 1) { $vk = [int][char]$key.ToUpper() }
    else { return }
    foreach ($m in $mods) {
        if ($modVk.ContainsKey($m)) { $mvk = $modVk[$m]; $W::keybd_event([byte]$mvk, 0, (Get-KF $mvk $false), [IntPtr]::Zero) }
    }
    $W::keybd_event([byte]$vk, 0, (Get-KF $vk $false), [IntPtr]::Zero)
    Start-Sleep -Milliseconds 30
    $W::keybd_event([byte]$vk, 0, (Get-KF $vk $true), [IntPtr]::Zero)
    [array]::Reverse($mods)
    foreach ($m in $mods) {
        if ($modVk.ContainsKey($m)) { $mvk = $modVk[$m]; $W::keybd_event([byte]$mvk, 0, (Get-KF $mvk $true), [IntPtr]::Zero) }
    }
}

Write-Output "READY"
"""

# Command templates sent to the persistent process
_PS_SCREENSHOT = """$b = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds; $bmp = New-Object System.Drawing.Bitmap($b.Width, $b.Height); $g = [System.Drawing.Graphics]::FromImage($bmp); $g.CopyFromScreen($b.Location, [System.Drawing.Point]::Empty, $b.Size); $bmp.Save('{path}', [System.Drawing.Imaging.ImageFormat]::Png); $g.Dispose(); $bmp.Dispose(); Write-Output "$($b.Width)x$($b.Height)" """

_PS_CLICK = """$W::SetCursorPos({x}, {y}); Start-Sleep -Milliseconds 30; $W::mouse_event({down},0,0,0,[IntPtr]::Zero); $W::mouse_event({up},0,0,0,[IntPtr]::Zero)"""

_PS_DOUBLE_CLICK = """$W::SetCursorPos({x}, {y}); Start-Sleep -Milliseconds 30; $W::mouse_event(0x0002,0,0,0,[IntPtr]::Zero); $W::mouse_event(0x0004,0,0,0,[IntPtr]::Zero); Start-Sleep -Milliseconds 60; $W::mouse_event(0x0002,0,0,0,[IntPtr]::Zero); $W::mouse_event(0x0004,0,0,0,[IntPtr]::Zero)"""

_PS_TYPE = """'{text}'.ToCharArray() | ForEach-Object {{ $vks=$W::VkKeyScan($_); $vk=$vks -band 0xFF; $sh=($vks -shr 8) -band 1; if($sh){{$W::keybd_event(0x10,0,0,[IntPtr]::Zero)}}; $W::keybd_event([byte]$vk,0,0,[IntPtr]::Zero); Start-Sleep -Milliseconds 20; $W::keybd_event([byte]$vk,0,$KU,[IntPtr]::Zero); if($sh){{$W::keybd_event(0x10,0,$KU,[IntPtr]::Zero)}}; Start-Sleep -Milliseconds 20 }}"""

_PS_KEY = """Do-Key '{combo}'"""

_PS_SCROLL = """if ({x} -gt 0 -or {y} -gt 0) {{ $W::SetCursorPos({x}, {y}); Start-Sleep -Milliseconds 30 }}; $delta = if ('{direction}' -eq 'up') {{ 120 }} else {{ -120 }}; 1..{clicks} | ForEach-Object {{ $W::mouse_event(0x0800,0,0,$delta,[IntPtr]::Zero); Start-Sleep -Milliseconds 30 }}"""

_PS_MOVE = """$W::SetCursorPos({x}, {y})"""

_PS_CLICK_HERE = """$W::mouse_event({down},0,0,0,[IntPtr]::Zero); $W::mouse_event({up},0,0,0,[IntPtr]::Zero)"""

_PS_DRAG = """$W::SetCursorPos({x1},{y1}); Start-Sleep -Milliseconds 50; $W::mouse_event(0x0002,0,0,0,[IntPtr]::Zero); 1..10 | ForEach-Object {{ $W::SetCursorPos([int]({x1}+({x2}-{x1})*$_/10),[int]({y1}+({y2}-{y1})*$_/10)); Start-Sleep -Milliseconds 10 }}; $W::mouse_event(0x0004,0,0,0,[IntPtr]::Zero)"""


class DesktopController:
    """Controls the desktop through screenshots and simulated input.

    Auto-detects WSL2 → uses a persistent PowerShell process for Windows desktop.
    Native Linux → uses scrot + xdotool.
    """

    def __init__(self):
        self._screen_width: int = 0
        self._screen_height: int = 0
        self._wsl: bool = _is_wsl2()
        self._ps_proc: asyncio.subprocess.Process | None = None
        self._last_click: tuple[int, int] | None = None
        self._last_move: tuple[int, int] | None = None

        if self._wsl:
            self._screenshot_path_win = r"C:\Users\Public\viewport_screenshot.png"
            self._screenshot_path_wsl = "/mnt/c/Users/Public/viewport_screenshot.png"
            print("[desktop] WSL2 detected — using PowerShell for Windows desktop", file=sys.stderr)
        else:
            self._screenshot_tool: str = ""
            self._input_tool: str = ""
            self._detect_linux_tools()
            print(f"[desktop] Linux — screenshot: {self._screenshot_tool or 'none'}, input: {self._input_tool or 'none'}", file=sys.stderr)

    # --- Setup ---

    def _detect_linux_tools(self):
        for tool in ("scrot", "maim", "grim", "import"):
            if shutil.which(tool):
                self._screenshot_tool = tool
                break
        for tool in ("xdotool", "ydotool"):
            if shutil.which(tool):
                self._input_tool = tool
                break

    async def _ensure_ps(self):
        """Start a persistent hidden PowerShell process if not already running."""
        if self._ps_proc and self._ps_proc.returncode is None:
            return

        # Write init script to file (avoids stdin escaping issues)
        init_path_wsl = "/mnt/c/Users/Public/viewport_ps_init.ps1"
        init_path_win = r"C:\Users\Public\viewport_ps_init.ps1"
        with open(init_path_wsl, "w", newline="\r\n") as f:
            f.write(_PS_INIT)

        self._ps_proc = await asyncio.create_subprocess_exec(
            "powershell.exe", "-NoProfile", "-NonInteractive",
            "-WindowStyle", "Hidden",
            "-ExecutionPolicy", "Bypass",
            "-Command", "-",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # Source the init script
        self._ps_proc.stdin.write(
            f". '{init_path_win}'\n".encode()
        )
        await self._ps_proc.stdin.drain()

        while True:
            line = await asyncio.wait_for(
                self._ps_proc.stdout.readline(), timeout=15
            )
            if b"READY" in line:
                break

        print("[desktop] PowerShell process ready", file=sys.stderr)

    async def _ps_exec(self, cmd: str) -> str:
        """Send a command to the persistent PowerShell process and return output."""
        await self._ensure_ps()

        marker = "##DONE##"
        full_cmd = cmd.strip() + f"\nWrite-Output '{marker}'\n"
        self._ps_proc.stdin.write(full_cmd.encode())
        await self._ps_proc.stdin.drain()

        lines = []
        while True:
            try:
                line = await asyncio.wait_for(
                    self._ps_proc.stdout.readline(), timeout=15
                )
            except asyncio.TimeoutError:
                # Read any stderr for diagnostics
                err = b""
                try:
                    err = await asyncio.wait_for(
                        self._ps_proc.stderr.read(4096), timeout=1
                    )
                except (asyncio.TimeoutError, Exception):
                    pass
                raise RuntimeError(
                    f"PowerShell command timed out.\nPartial output: {''.join(lines)}\nStderr: {err.decode(errors='replace')}"
                )
            if not line:
                break  # EOF
            text = line.decode().rstrip("\r\n")
            if marker in text:
                break
            lines.append(text)

        return "\n".join(lines)

    def check_tools(self) -> str | None:
        """Return error message if required tools are missing, None if OK."""
        if self._wsl:
            if not shutil.which("powershell.exe"):
                return "powershell.exe not found (required for WSL2 desktop control)"
            return None
        missing = []
        if not self._screenshot_tool:
            missing.append("screenshot tool (install: scrot, maim, grim, or imagemagick)")
        if not self._input_tool:
            missing.append("input tool (install: xdotool or ydotool)")
        return "Missing: " + "; ".join(missing) if missing else None

    @property
    def screen_size(self) -> tuple[int, int]:
        return (self._screen_width, self._screen_height)

    # --- Screenshot ---

    async def screenshot(self) -> bytes:
        """Take a full desktop screenshot. Returns PNG bytes."""
        if self._wsl:
            return await self._screenshot_win()
        return await self._screenshot_linux()

    async def _screenshot_linux(self) -> bytes:
        fd, path = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        try:
            if self._screenshot_tool == "scrot":
                await self._run("scrot", "-o", "-F", path)
            elif self._screenshot_tool == "maim":
                await self._run("maim", path)
            elif self._screenshot_tool == "grim":
                await self._run("grim", path)
            elif self._screenshot_tool == "import":
                await self._run("import", "-window", "root", path)
            else:
                raise RuntimeError("No screenshot tool. Install: sudo apt install scrot xdotool")
            with open(path, "rb") as f:
                png = f.read()
            img = Image.open(io.BytesIO(png))
            self._screen_width, self._screen_height = img.size
            return png
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    async def _screenshot_win(self) -> bytes:
        result = await self._ps_exec(
            _PS_SCREENSHOT.format(path=self._screenshot_path_win)
        )
        for line in result.strip().splitlines():
            if "x" in line:
                try:
                    w, h = line.strip().split("x")
                    self._screen_width, self._screen_height = int(w), int(h)
                except ValueError:
                    pass
        with open(self._screenshot_path_wsl, "rb") as f:
            return f.read()

    # --- Mouse ---

    _BUTTON_FLAGS = {1: (0x0002, 0x0004), 2: (0x0020, 0x0040), 3: (0x0008, 0x0010)}

    async def click(self, x: int, y: int, button: int = 1):
        down, up = self._BUTTON_FLAGS.get(button, (0x0002, 0x0004))
        if self._wsl:
            await self._ps_exec(_PS_CLICK.format(x=x, y=y, down=down, up=up))
        else:
            await self._run("xdotool", "mousemove", "--sync", str(x), str(y))
            await self._run("xdotool", "click", str(button))
        if button == 1:
            self._last_click = (x, y)

    async def double_click(self, x: int, y: int):
        if self._wsl:
            await self._ps_exec(_PS_DOUBLE_CLICK.format(x=x, y=y))
        else:
            await self._run("xdotool", "mousemove", "--sync", str(x), str(y))
            await self._run("xdotool", "click", "--repeat", "2", "--delay", "100", "1")

    async def right_click(self, x: int, y: int):
        await self.click(x, y, button=3)

    async def move_mouse(self, x: int, y: int):
        """Move cursor to (x, y) without clicking."""
        if self._wsl:
            await self._ps_exec(_PS_MOVE.format(x=x, y=y))
        else:
            await self._run("xdotool", "mousemove", "--sync", str(x), str(y))
        self._last_move = (x, y)

    async def click_here(self, button: int = 1):
        """Click at the current mouse position (no move)."""
        down, up = self._BUTTON_FLAGS.get(button, (0x0002, 0x0004))
        if self._wsl:
            await self._ps_exec(_PS_CLICK_HERE.format(down=down, up=up))
        else:
            await self._run("xdotool", "click", str(button))
        if button == 1 and self._last_move:
            self._last_click = self._last_move

    # --- Keyboard ---

    async def type_text(self, text: str):
        if self._wsl:
            # Escape single quotes for PowerShell string
            escaped = text.replace("'", "''")
            if self._last_click:
                # Re-click last position to restore focus (may have been lost
                # by the screenshot that followed the original click)
                lx, ly = self._last_click
                refocus = _PS_CLICK.format(
                    x=lx, y=ly, down=0x0002, up=0x0004
                ) + "; Start-Sleep -Milliseconds 50; "
                await self._ps_exec(
                    refocus + _PS_TYPE.format(text=escaped)
                )
            else:
                await self._ps_exec(_PS_TYPE.format(text=escaped))
        else:
            if self._last_click:
                lx, ly = self._last_click
                await self._run("xdotool", "mousemove", "--sync", str(lx), str(ly))
                await self._run("xdotool", "click", "1")
            await self._run("xdotool", "type", "--delay", "30", "--", text)

    async def key(self, combo: str):
        if self._wsl:
            escaped = combo.replace("'", "''")
            await self._ps_exec(_PS_KEY.format(combo=escaped))
        else:
            await self._run("xdotool", "key", "--", combo)

    # --- Scroll ---

    async def scroll(self, direction: str, x: int = 0, y: int = 0, clicks: int = 5):
        if self._wsl:
            await self._ps_exec(
                _PS_SCROLL.format(x=x, y=y, direction=direction, clicks=clicks)
            )
        else:
            if x > 0 or y > 0:
                await self._run("xdotool", "mousemove", "--sync", str(x), str(y))
            button = "4" if direction == "up" else "5"
            await self._run("xdotool", "click", "--repeat", str(clicks), "--delay", "30", button)

    # --- Drag ---

    async def drag(self, x1: int, y1: int, x2: int, y2: int):
        if self._wsl:
            await self._ps_exec(
                _PS_DRAG.format(x1=x1, y1=y1, x2=x2, y2=y2)
            )
        else:
            await self._run("xdotool", "mousemove", "--sync", str(x1), str(y1))
            await self._run("xdotool", "mousedown", "1")
            await asyncio.sleep(0.05)
            steps = 10
            for i in range(1, steps + 1):
                ix = x1 + (x2 - x1) * i // steps
                iy = y1 + (y2 - y1) * i // steps
                await self._run("xdotool", "mousemove", "--sync", str(ix), str(iy))
                await asyncio.sleep(0.01)
            await self._run("xdotool", "mouseup", "1")

    # --- Command runner (Linux) ---

    async def _run(self, *cmd: str) -> str:
        """Run a local command."""
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"Command failed: {' '.join(cmd)}\n{stderr.decode().strip()}")
        return stdout.decode()
