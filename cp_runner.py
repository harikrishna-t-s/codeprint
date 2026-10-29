"""Language detection, source loading and subprocess execution.

SECURITY NOTE: running a program in a subprocess is NOT a security sandbox.
The child runs with your user's permissions and can read/write your files and use
the network. Only run code you trust. What we do for basic hygiene:
  * no shell (argument lists only), absolute paths, fixed command templates
  * a throw-away working directory (deleted afterwards) unless --cwd-source is used
  * wall-clock timeout, the whole process group is killed on timeout
  * CPU/file-size/core-dump resource limits
  * stdin is /dev/null unless an input file is given
  * refuses to run as root unless it can drop to the 'nobody' account
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from cp_config import CodePrintError, EXIT_ENV, EXIT_NOT_FOUND, EXIT_USAGE


@dataclass(frozen=True)
class Language:
    key: str
    display: str
    extensions: tuple
    lexer: str
    tools: tuple          # executables that must exist to run this language
    shebangs: tuple = ()


LANGUAGES: dict[str, Language] = {
    "python": Language("python", "Python", (".py",), "python", ("python3",), ("python",)),
    "c": Language("c", "C", (".c",), "c", ("gcc|cc",)),
    "cpp": Language("cpp", "C++", (".cpp", ".cc", ".cxx", ".c++"), "cpp", ("g++|c++",)),
    "java": Language("java", "Java", (".java",), "java", ("javac", "java")),
    "javascript": Language("javascript", "JavaScript (Node.js)", (".js", ".mjs", ".cjs"),
                           "javascript", ("node|nodejs",), ("node", "nodejs")),
    "bash": Language("bash", "Bash", (".sh", ".bash"), "bash", ("bash",), ("bash", "sh")),
}
ALIASES = {"py": "python", "python3": "python", "c++": "cpp", "cc": "cpp", "js": "javascript",
           "node": "javascript", "nodejs": "javascript", "sh": "bash", "shell": "bash"}


def find_tool(spec: str) -> str | None:
    """`spec` is 'a|b' meaning 'a or b'. Returns the first executable found."""
    for name in spec.split("|"):
        path = shutil.which(name)
        if path:
            return path
    return None


def missing_tools(lang: Language) -> list[str]:
    return [spec.replace("|", " or ") for spec in lang.tools if not find_tool(spec)]


def language_by_name(name: str) -> Language:
    key = ALIASES.get(name.strip().lower(), name.strip().lower())
    if key not in LANGUAGES:
        raise CodePrintError("UNSUPPORTED_LANGUAGE",
                             f"Unsupported language '{name}'. Supported: {', '.join(LANGUAGES)}", EXIT_USAGE)
    return LANGUAGES[key]


def detect_language(path: Path, explicit: str | None = None) -> Language:
    if explicit:
        return language_by_name(explicit)
    ext = Path(path).suffix.lower()
    for lang in LANGUAGES.values():
        if ext in lang.extensions:
            return lang
    if not ext:  # try a shebang for extension-less scripts
        try:
            with open(path, "rb") as fh:
                first = fh.readline(200).decode("utf-8", "replace")
        except OSError:
            first = ""
        if first.startswith("#!"):
            for lang in LANGUAGES.values():
                if any(s in first for s in lang.shebangs):
                    return lang
    raise CodePrintError("UNSUPPORTED_LANGUAGE",
                         f"Cannot detect the language of '{Path(path).name}'. "
                         f"Use --language ({', '.join(LANGUAGES)}).", EXIT_USAGE)


def read_source(path: Path) -> str:
    """Read a source file (read-only). UTF-8 required; CRLF normalised to LF."""
    path = Path(path)
    if not path.exists():
        raise CodePrintError("FILE_NOT_FOUND", f"Source file not found: {path}", EXIT_NOT_FOUND)
    if not path.is_file():
        raise CodePrintError("NOT_A_FILE", f"Not a regular file: {path}", EXIT_USAGE)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise CodePrintError("READ_FAILED", f"Cannot read {path}: {exc}", EXIT_USAGE)
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise CodePrintError("INVALID_ENCODING", f"{path} is not valid UTF-8; convert it first.", EXIT_USAGE)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        raise CodePrintError("EMPTY_FILE", f"Source file is empty: {path}", EXIT_USAGE)
    return text


# ---------------------------------------------------------------------------
@dataclass
class RunResult:
    status: str = "not_run"       # ok | failed | timeout | compile_error | not_run
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    compile_output: str = ""
    duration_s: float = 0.0
    truncated: bool = False
    timeout_s: float | None = None
    notes: list = field(default_factory=list)


def _drop_privileges_target():
    """If we are root, return (uid, gid) of 'nobody' or raise; otherwise None."""
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        return None
    try:
        import pwd
        pw = pwd.getpwnam("nobody")
        return pw.pw_uid, pw.pw_gid
    except (ImportError, KeyError):
        raise CodePrintError("REFUSE_ROOT",
                             "Refusing to run programs as root (no 'nobody' account to drop to). "
                             "Run codeprint as a normal user.", EXIT_ENV)


def _make_preexec(timeout: float, drop):
    import resource

    def preexec():
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_FSIZE, (64 << 20, 64 << 20))
        cpu = int(timeout * 2) + 5
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
        if drop:
            os.setgroups([])
            os.setgid(drop[1])
            os.setuid(drop[0])
    return preexec


def _run(cmd, cwd, stdin_path, timeout, max_bytes, env, drop):
    """Run one command without a shell. Returns (exit_code|None, out, err, secs, timed_out, truncated)."""
    with tempfile.TemporaryFile() as fout, tempfile.TemporaryFile() as ferr:
        stdin = open(stdin_path, "rb") if stdin_path else subprocess.DEVNULL
        start = time.perf_counter()
        timed_out = False
        try:
            proc = subprocess.Popen(cmd, cwd=cwd, stdin=stdin, stdout=fout, stderr=ferr, env=env,
                                    shell=False, start_new_session=True,
                                    preexec_fn=_make_preexec(timeout, drop))
        except OSError as exc:
            raise CodePrintError("EXEC_FAILED", f"Could not start {cmd[0]}: {exc}", EXIT_ENV)
        finally:
            if stdin_path:
                stdin.close()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
        # kill anything left in the process group (timeouts, background children)
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        proc.wait()
        secs = time.perf_counter() - start
        truncated = False
        outs = []
        for fh in (fout, ferr):
            fh.seek(0)
            data = fh.read(max_bytes + 1)
            if len(data) > max_bytes:
                data = data[:max_bytes]
                truncated = True
            outs.append(data.decode("utf-8", errors="replace"))
        code = None if timed_out else proc.returncode
        return code, outs[0], outs[1], secs, timed_out, truncated


def run_program(lang: Language, source_path: Path, input_file: Path | None = None,
                timeout: float = 10.0, cwd_source: bool = False,
                max_output_kb: int = 256) -> RunResult:
    """Compile (if needed) and run `source_path`; never raises for program failures."""
    source_path = Path(source_path).resolve()
    if not source_path.is_file():
        raise CodePrintError("FILE_NOT_FOUND", f"Source file not found: {source_path}", EXIT_NOT_FOUND)
    missing = missing_tools(lang)
    if missing:
        raise CodePrintError("TOOL_MISSING",
                             f"{lang.display} needs {', '.join(missing)}, which was not found on PATH. "
                             f"Install it or use --code-only.", EXIT_ENV)
    if input_file is not None:
        input_file = Path(input_file)
        if not input_file.is_file():
            raise CodePrintError("FILE_NOT_FOUND", f"Input file not found: {input_file}", EXIT_NOT_FOUND)
        input_file = input_file.resolve()
    drop = _drop_privileges_target()
    max_bytes = max_output_kb * 1024

    work = Path(tempfile.mkdtemp(prefix="codeprint-run-"))
    result = RunResult(timeout_s=timeout)
    try:
        if drop:  # unprivileged child must be able to use the work dir
            os.chmod(work, 0o777)
        # Work on a copy unless the user wants the original directory as cwd.
        if cwd_source:
            src, run_cwd = source_path, source_path.parent
        else:
            src = work / source_path.name
            shutil.copy2(source_path, src)
            os.chmod(src, 0o644)
            run_cwd = work
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1", TMPDIR=str(work))
        if drop:
            env["HOME"] = str(work)

        compile_cmd, run_cmd = _commands(lang, src, work)
        if compile_cmd:
            code, out, err, _, timed_out, trunc = _run(compile_cmd, str(work), None, 60, max_bytes, env, drop)
            result.compile_output = _tidy(out + err, work).strip("\n")
            if timed_out or code != 0:
                result.status = "compile_error"
                result.exit_code = code
                if timed_out:
                    result.compile_output += "\n[compiler timed out after 60s]"
                return result
        code, out, err, secs, timed_out, trunc = _run(run_cmd, str(run_cwd), input_file, timeout,
                                                       max_bytes, env, drop)
        result.stdout, result.stderr = _tidy(out, work), _tidy(err, work)
        result.duration_s, result.truncated, result.exit_code = secs, trunc, code
        result.status = "timeout" if timed_out else ("ok" if code == 0 else "failed")
        return result
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _tidy(text: str, work: Path) -> str:
    """Hide the throw-away directory name so diagnostics read `prog.c:4:` not `/tmp/codeprint-run-x/prog.c:4:`."""
    return text.replace(str(work) + os.sep, "")


def _commands(lang: Language, src: Path, work: Path):
    """Return (compile_cmd|None, run_cmd) as argument lists."""
    key = lang.key
    if key == "python":
        return None, [find_tool("python3"), str(src)]
    if key == "bash":
        return None, [find_tool("bash"), str(src)]
    if key == "javascript":
        return None, [find_tool("node|nodejs"), str(src)]
    if key == "c":
        exe = work / "prog.out"
        return [find_tool("gcc|cc"), str(src), "-o", str(exe), "-lm"], [str(exe)]
    if key == "cpp":
        exe = work / "prog.out"
        return [find_tool("g++|c++"), "-std=c++17", str(src), "-o", str(exe)], [str(exe)]
    if key == "java":
        classes = work / "classes"
        classes.mkdir(exist_ok=True)
        os.chmod(classes, 0o777)
        return ([find_tool("javac"), "-encoding", "UTF-8", "-d", str(classes), str(src)],
                [find_tool("java"), "-cp", str(classes), "-Dfile.encoding=UTF-8", src.stem])
    raise CodePrintError("UNSUPPORTED_LANGUAGE", f"No run recipe for {key}", EXIT_USAGE)
