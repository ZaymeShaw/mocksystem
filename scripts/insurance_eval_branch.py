#!/usr/bin/env python3
"""保险问答评测分支维护工具（仅本地；禁止任何 push）。

分层约定（自底向上）：
  UPSTREAM（默认 origin/main，同事维护）
    └─ 可合入上游的提交（如评测头透传，不带标记）
        └─ 仅本地提交（subject 以 LOCAL_MARKER 开头，如 :4002 中转配置）
  CURRENT（默认 eval/current）= 评测工作树 EVAL_WT 检出的稳定分支

子命令：
  status        查看上游/当前分支/落后提交/我方提交/工作树与进程状态
  sync          上游更新后，在 /tmp 临时工作树里把我方提交逐个 cherry-pick
                到新上游，测试通过后再移动 CURRENT；冲突或测试失败时 CURRENT 不动
  export-patch  仅导出不带标记的（可合入上游的）提交为补丁，并做敏感信息扫描
  rollback      列出 eval/sync-* 历史分支；指定分支时把 CURRENT 指回该分支

仅使用 Python 3 标准库；可用系统 python3 运行。示例：
  python3 insurance_eval_branch.py status --fetch
  python3 insurance_eval_branch.py sync --dry-run
  python3 insurance_eval_branch.py sync --full-tests
  python3 insurance_eval_branch.py export-patch
  python3 insurance_eval_branch.py rollback eval/sync-20260925
"""
from __future__ import annotations

import argparse
import datetime as _dt
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# ---------------------------------------------------------------- 默认配置（可用命令行覆盖）
REPO = "/Users/xiaozijian/WorkSpace/package/insurance_qa_agent/insurance-qa-agent"
EVAL_WT = "/Users/xiaozijian/WorkSpace/package/insurance_qa_agent/insurance-qa-agent-eval"
UPSTREAM = "origin/main"
CURRENT = "eval/current"
LOCAL_MARKER = "local(eval):"
TEST_PYTHON = REPO + "/.feval_venv/bin/python"
TEST_TARGET = "tests/observability/test_eval_passthrough.py"
FULL_SUITE_IGNORES = ["tests/observability/test_langfuse_live.py"]
PATCH_ROOT = "/Users/xiaozijian/WorkSpace/package/mock_system/eval_runs/_patches"
SYNC_PREFIX = "eval/sync-"
MAX_OUR_COMMITS = 20  # 我方提交数超过此值视为异常（例如上游被改写），拒绝继续

EXIT_OK, EXIT_ERROR, EXIT_CONFLICT, EXIT_TESTS = 0, 1, 2, 3
SHELL_NAMES = {"zsh", "-zsh", "bash", "-bash", "sh", "-sh", "fish", "login", "tmux", "screen"}


class Abort(Exception):
    """带退出码的中止。"""

    def __init__(self, message: str, code: int = EXIT_ERROR) -> None:
        super().__init__(message)
        self.code = code


def info(message: str = "") -> None:
    print(message, flush=True)


def warn(message: str) -> None:
    print(f"[警告] {message}", flush=True)


# ---------------------------------------------------------------- git 封装
def git(args: list[str], cwd: str, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    # 硬性护栏：任何含 push 的 git 参数一律拒绝执行（本工具只做本地操作）。
    if any("push" in str(arg).lower() for arg in args):
        raise Abort(f"安全护栏：拒绝执行包含 push 的 git 命令：git {' '.join(args)}")
    proc = subprocess.run(["git", *args], cwd=cwd, text=True,
                          stdout=subprocess.PIPE if capture else None,
                          stderr=subprocess.PIPE if capture else None)
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip() if capture else ""
        raise Abort(f"git {' '.join(args)} 失败（退出码 {proc.returncode}）：{detail}")
    return proc


def out(args: list[str], cwd: str) -> str:
    return git(args, cwd).stdout.strip()


def ref_exists(ref: str, cwd: str) -> bool:
    return git(["rev-parse", "-q", "--verify", f"{ref}^{{commit}}"], cwd, check=False).returncode == 0


def branch_exists(name: str, cwd: str) -> bool:
    return git(["show-ref", "--verify", "-q", f"refs/heads/{name}"], cwd, check=False).returncode == 0


def short(sha: str, cwd: str) -> str:
    return out(["rev-parse", "--short", sha], cwd)


def subject(sha: str, cwd: str) -> str:
    return out(["log", "-1", "--format=%s", sha], cwd)


def unset_upstream(branch: str, cwd: str) -> None:
    git(["branch", "--unset-upstream", branch], cwd, check=False)


# ---------------------------------------------------------------- 上下文
class Ctx:
    def __init__(self, ns: argparse.Namespace) -> None:
        self.repo = str(Path(ns.repo).resolve())
        self.wt = str(Path(ns.wt).resolve())
        self.upstream = ns.upstream
        self.current = ns.current
        self.marker = ns.marker
        self.python = ns.python
        self.test_target = ns.test_target
        if not Path(self.repo, ".git").exists():
            raise Abort(f"主仓库不存在或不是 git 仓库：{self.repo}")

    def remote_of_upstream(self) -> str | None:
        full = git(["rev-parse", "--symbolic-full-name", self.upstream], self.repo, check=False).stdout.strip()
        match = re.match(r"refs/remotes/([^/]+)/", full)
        return match.group(1) if match else None

    def fetch(self) -> None:
        remote = self.remote_of_upstream()
        if remote is None:
            info(f"上游 {self.upstream} 不是远程跟踪分支，跳过 fetch。")
            return
        info(f"git fetch {remote}（只读，更新远程跟踪分支）…")
        git(["fetch", "--no-tags", remote], self.repo, capture=False)

    def is_local(self, sha: str) -> bool:
        return subject(sha, self.repo).startswith(self.marker)

    def our_commits(self, current: str | None = None) -> list[str]:
        """CURRENT 相对 UPSTREAM 的我方提交（旧序）；排除合并提交与已被上游等价合入的补丁。"""
        current = current or self.current
        rng = f"{self.upstream}...{current}"
        merges = out(["rev-list", "--merges", "--right-only", rng], self.repo).split()
        if merges:
            warn(f"我方范围内含 {len(merges)} 个合并提交（已排除，可能是上游改写或误合并）："
                 + ", ".join(short(m, self.repo) for m in merges[:5]))
        commits = out(["rev-list", "--reverse", "--no-merges", "--right-only", "--cherry-pick", rng],
                      self.repo).split()
        if len(commits) > MAX_OUR_COMMITS:
            raise Abort(f"我方提交多达 {len(commits)} 个，超过阈值 {MAX_OUR_COMMITS}，疑似上游历史被改写，请人工检查。")
        return commits

    def patch_equivalent_upstream(self, current: str | None = None) -> list[str]:
        """已被上游以等价补丁合入的我方提交（sync 时将被跳过）。"""
        current = current or self.current
        lines = out(["log", "--right-only", "--no-merges", "--cherry-mark", "--format=%m %H",
                     f"{self.upstream}...{current}"], self.repo).splitlines()
        return [line.split()[1] for line in lines if line.startswith("=")]

    def label(self, sha: str) -> str:
        return "[local-only]" if self.is_local(sha) else "[upstream-mergeable]"


# ---------------------------------------------------------------- 工作树与进程检查
def worktree_state(wt: str) -> tuple[list[str], list[str], str]:
    if not Path(wt).exists():
        raise Abort(f"评测工作树不存在：{wt}")
    lines = git(["status", "--porcelain"], wt).stdout.splitlines()
    tracked = [line for line in lines if not line.startswith("??")]
    untracked = [line for line in lines if line.startswith("??")]
    head = git(["symbolic-ref", "-q", "--short", "HEAD"], wt, check=False).stdout.strip() or "(detached HEAD)"
    return tracked, untracked, head


def processes_using(path: str) -> list[tuple[int, str, str, bool]]:
    """返回 (pid, 来源, 命令, 是否仅为交互 shell)；来源为 cmdline 或 cwd。"""
    real = os.path.realpath(path)
    pattern = re.compile("(" + re.escape(path) + "|" + re.escape(real) + r")(?=/|\s|$|['\"])")
    skip = {os.getpid(), os.getppid()}
    found: dict[int, tuple[int, str, str, bool]] = {}
    commands: dict[int, str] = {}
    ps = subprocess.run(["ps", "-axww", "-o", "pid=,command="], text=True, stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL)
    for line in ps.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        pid, command = int(parts[0]), parts[1]
        commands[pid] = command
        if pid not in skip and pattern.search(command) and "insurance_eval_branch.py" not in command:
            found[pid] = (pid, "cmdline", command, False)
    if shutil.which("lsof"):
        try:
            proc = subprocess.run(["lsof", "-w", "-a", "-d", "cwd", "-Fpn"], text=True, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, timeout=30)
            pid = None
            for line in proc.stdout.splitlines():
                if line.startswith("p"):
                    pid = int(line[1:])
                elif line.startswith("n") and pid is not None and pid not in skip and pid not in found:
                    cwd = line[1:]
                    if cwd == real or cwd.startswith(real + "/") or cwd == path or cwd.startswith(path + "/"):
                        command = commands.get(pid, "?")
                        exe = os.path.basename(command.split()[0]) if command.split() else ""
                        found[pid] = (pid, "cwd", command, exe in SHELL_NAMES)
        except (subprocess.TimeoutExpired, OSError):
            warn("lsof 检查超时或失败，仅依据命令行判断。")
    return sorted(found.values())


def print_processes(procs: list[tuple[int, str, str, bool]]) -> None:
    if not procs:
        info("  无进程从评测工作树运行。")
        return
    for pid, source, command, is_shell in procs:
        tag = "（交互 shell，仅提示）" if is_shell else ""
        info(f"  PID {pid} [{source}] {command[:160]}{tag}")


def safety_check(ctx: Ctx, force: bool) -> None:
    """移动 CURRENT 前的检查：工作树有已跟踪改动、或有非 shell 进程从工作树运行时拒绝（--force 可越过）。"""
    tracked, untracked, head = worktree_state(ctx.wt)
    procs = processes_using(ctx.wt)
    blocking = [p for p in procs if not p[3]]
    problems = []
    if tracked:
        problems.append(f"评测工作树有 {len(tracked)} 处已跟踪改动未提交")
    if blocking:
        problems.append(f"有 {len(blocking)} 个进程从评测工作树运行")
    if untracked:
        warn(f"评测工作树有 {len(untracked)} 个未跟踪文件（不阻断）。")
    if head != ctx.current:
        warn(f"评测工作树当前检出的是 {head}，而非 {ctx.current}；更新后将切换到 {ctx.current}。")
    if problems:
        for line in tracked[:10]:
            info(f"  {line}")
        print_processes(procs)
        if not force:
            raise Abort("拒绝移动 " + ctx.current + "：" + "；".join(problems) + "。确认无误后可加 --force。")
        warn("已指定 --force，忽略：" + "；".join(problems))


def backup_current_if_orphan(ctx: Ctx) -> str | None:
    """若 CURRENT 旧提交不在任何其他 eval/* 分支顶端，先留一个 eval/sync-prev-* 以便回滚。"""
    if not branch_exists(ctx.current, ctx.repo):
        return None
    old = out(["rev-parse", ctx.current], ctx.repo)
    tips = out(["for-each-ref", "--format=%(objectname) %(refname:short)", "refs/heads/eval/"], ctx.repo)
    for line in tips.splitlines():
        sha, name = line.split(" ", 1)
        if sha == old and name != ctx.current:
            return None
    name = unique_branch(ctx, SYNC_PREFIX + "prev-" + _dt.datetime.now().strftime("%Y%m%d-%H%M"))
    git(["branch", "--no-track", name, old], ctx.repo)
    unset_upstream(name, ctx.repo)
    info(f"旧 {ctx.current}（{short(old, ctx.repo)}）未被其他分支引用，已备份为 {name}。")
    return name


def move_current(ctx: Ctx, target: str) -> None:
    git(["checkout", "-B", ctx.current, target], ctx.wt)
    unset_upstream(ctx.current, ctx.repo)


def unique_branch(ctx: Ctx, base: str) -> str:
    name = base
    if branch_exists(name, ctx.repo):
        name = f"{base}-{_dt.datetime.now().strftime('%H%M')}"
    index = 2
    while branch_exists(name, ctx.repo):
        name = f"{base}-{_dt.datetime.now().strftime('%H%M')}-{index}"
        index += 1
    return name


# ---------------------------------------------------------------- 测试
def pytest_failures(output: str) -> set[str]:
    failures = set()
    for line in output.splitlines():
        match = re.match(r"^(FAILED|ERROR) (\S+)", line)
        if match:
            failures.add(match.group(2))
    return failures


def run_pytest(ctx: Ctx, cwd: str, args: list[str], log: Path) -> tuple[int, str]:
    env = dict(os.environ, PYTHONPATH=cwd)
    info(f"运行测试：{ctx.python} -m pytest {' '.join(args)}（cwd={cwd}，日志 {log}）")
    with open(log, "w", encoding="utf-8") as handle:
        proc = subprocess.run([ctx.python, "-m", "pytest", "-p", "no:cacheprovider", "-rfE", *args],
                              cwd=cwd, env=env, text=True, stdout=handle, stderr=subprocess.STDOUT)
    text = log.read_text(encoding="utf-8", errors="replace")
    tail = [line for line in text.splitlines() if line.strip()][-3:]
    for line in tail:
        info(f"  | {line[:200]}")
    return proc.returncode, text


def rerun_at(ctx: Ctx, ref: str, tests: list[str], log: Path) -> set[str]:
    """在 ref 的临时分离工作树上复跑指定用例，返回仍失败的用例。"""
    base_dir = tempfile.mkdtemp(prefix="iqa-sync-base-", dir="/tmp")
    base_wt = str(Path(base_dir, "wt"))
    try:
        git(["worktree", "add", "--detach", base_wt, ref], ctx.repo)
        _, text = run_pytest(ctx, base_wt, sorted(tests), log)
        return pytest_failures(text)
    finally:
        git(["worktree", "remove", "--force", base_wt], ctx.repo, check=False)
        git(["worktree", "prune"], ctx.repo, check=False)
        shutil.rmtree(base_dir, ignore_errors=True)


def mergeable_only_ref(ctx: Ctx, tmp_wt: str) -> str | None:
    """同步分支上“上游 + 可合入提交”的状态（首个仅本地提交的父提交）；无本地提交或二者交错时返回 None。"""
    commits = out(["rev-list", "--reverse", f"{ctx.upstream}..HEAD"], tmp_wt).split()
    flags = [ctx.is_local(sha) for sha in commits]
    if True not in flags:
        return None
    first = flags.index(True)
    if not all(flags[first:]):
        warn("可合入提交与仅本地提交交错，无法单独验证可合入部分。")
        return None
    return out(["rev-parse", f"{commits[first]}^"], tmp_wt)


def run_tests(ctx: Ctx, tmp_wt: str, full: bool, logdir: Path, strict_local: bool = False) -> bool:
    if not Path(ctx.python).exists():
        raise Abort(f"测试解释器不存在：{ctx.python}")
    if not Path(tmp_wt, ctx.test_target).exists():
        info(f"[失败] 新分支中找不到透传测试 {ctx.test_target}。")
        return False
    code, _ = run_pytest(ctx, tmp_wt, [ctx.test_target], logdir / "passthrough.log")
    if code != 0:
        info("[失败] 透传测试未通过。")
        return False
    info("透传测试通过。")
    if not full:
        return True
    ignores = [f"--ignore={path}" for path in FULL_SUITE_IGNORES]
    code, text = run_pytest(ctx, tmp_wt, ignores, logdir / "full.log")
    if code == 0:
        info("全量测试通过。")
        return True
    failures = pytest_failures(text)
    if not failures:
        info("[失败] 全量测试退出码非零且无法解析失败用例，请查看日志。")
        return False
    # 与纯上游对比：只在上游同样失败的用例视为环境性存量失败。
    info(f"全量测试有 {len(failures)} 个失败/错误，在纯上游 {ctx.upstream} 上复跑这些用例做对比…")
    base_failures = rerun_at(ctx, ctx.upstream, sorted(failures), logdir / "full_upstream_rerun.log")
    new = failures - base_failures
    info(f"其中 {len(failures & base_failures)} 个在纯上游同样失败（环境性存量）。")
    if not new:
        info("全量测试：无新引入失败，视为通过。")
        return True
    # 再区分：新失败是否仅由本地配置提交引起（在“上游 + 可合入提交”状态上不失败）。
    local_caused: set[str] = set()
    middle = mergeable_only_ref(ctx, tmp_wt)
    if middle is not None:
        info(f"有 {len(new)} 个新失败，在“上游 + 可合入提交”状态 {short(middle, ctx.repo)} 上复跑以定位来源…")
        middle_failures = rerun_at(ctx, middle, sorted(new), logdir / "full_mergeable_rerun.log")
        local_caused = new - middle_failures
    real = sorted(new - local_caused)
    if local_caused:
        warn(f"{len(local_caused)} 个失败仅由仅本地提交（{ctx.marker}）引起，属本地配置与上游约定不一致的预期现象：")
        for item in sorted(local_caused)[:30]:
            info(f"  {item}")
    if real:
        info(f"[失败] 有 {len(real)} 个失败由可合入提交或同步引入：")
        for item in real[:30]:
            info(f"  {item}")
        return False
    if local_caused and strict_local:
        info("[失败] 已指定 --strict-local，本地提交引起的失败也视为不通过。")
        return False
    info("全量测试：无可合入提交引入的失败，视为通过。")
    return True


# ---------------------------------------------------------------- 子命令
def cmd_status(ctx: Ctx, ns: argparse.Namespace) -> int:
    if ns.fetch:
        ctx.fetch()
    if not ref_exists(ctx.upstream, ctx.repo):
        raise Abort(f"上游引用不存在：{ctx.upstream}")
    info(f"上游 {ctx.upstream}：{out(['log', '-1', '--format=%h  %ci  %s', ctx.upstream], ctx.repo)}")
    if not ref_exists(ctx.current, ctx.repo):
        info(f"当前分支 {ctx.current} 不存在。")
        return EXIT_ERROR
    info(f"当前 {ctx.current}：{out(['log', '-1', '--format=%h  %ci  %s', ctx.current], ctx.repo)}")
    base = out(["merge-base", ctx.upstream, ctx.current], ctx.repo)
    info(f"基点 merge-base：{out(['log', '-1', '--format=%h  %ci  %s', base], ctx.repo)}")
    behind = out(["rev-list", "--count", f"{ctx.current}..{ctx.upstream}"], ctx.repo)
    info(f"{ctx.current} 落后上游 {behind} 个提交" + ("（已是最新）" if behind == "0" else "："))
    if behind != "0":
        lines = out(["log", "--oneline", "--no-decorate", f"{ctx.current}..{ctx.upstream}"], ctx.repo).splitlines()
        for line in lines[:15]:
            info(f"  {line}")
        if len(lines) > 15:
            info(f"  …（另有 {len(lines) - 15} 个）")
    commits = ctx.our_commits()
    merged = set(ctx.patch_equivalent_upstream())
    info(f"我方提交（{ctx.upstream}..{ctx.current}，旧→新）共 {len(commits)} 个：")
    for sha in commits:
        info(f"  {short(sha, ctx.repo)} {ctx.label(sha):22} {subject(sha, ctx.repo)}")
    for sha in merged:
        info(f"  {short(sha, ctx.repo)} [已被上游等价合入]    {subject(sha, ctx.repo)}")
    try:
        tracked, untracked, head = worktree_state(ctx.wt)
        state = "干净" if not tracked else f"有 {len(tracked)} 处已跟踪改动"
        info(f"评测工作树 {ctx.wt}：检出 {head}，{state}，未跟踪文件 {len(untracked)} 个")
        info("从评测工作树运行的进程（仅提示）：")
        print_processes(processes_using(ctx.wt))
    except Abort as exc:
        warn(str(exc))
    return EXIT_OK


def cmd_sync(ctx: Ctx, ns: argparse.Namespace) -> int:
    if ns.dry_run:
        info("[dry-run] 不 fetch、不创建分支/工作树、不移动任何引用；以下为基于本地引用的计划。")
    elif not ns.no_fetch:
        ctx.fetch()
    for ref in (ctx.upstream, ctx.current):
        if not ref_exists(ref, ctx.repo):
            raise Abort(f"引用不存在：{ref}")
    old_current = out(["rev-parse", ctx.current], ctx.repo)
    upstream_sha = out(["rev-parse", ctx.upstream], ctx.repo)
    info(f"上游 {ctx.upstream} = {short(upstream_sha, ctx.repo)}；{ctx.current} = {short(old_current, ctx.repo)}")
    if git(["merge-base", "--is-ancestor", upstream_sha, old_current], ctx.repo, check=False).returncode == 0:
        info(f"{ctx.current} 已包含上游 {ctx.upstream}，无需同步。")
        return EXIT_OK
    commits = ctx.our_commits()
    merged = ctx.patch_equivalent_upstream()
    info(f"待重放的我方提交 {len(commits)} 个（旧→新）：")
    for sha in commits:
        info(f"  {short(sha, ctx.repo)} {ctx.label(sha):22} {subject(sha, ctx.repo)}")
    for sha in merged:
        info(f"  {short(sha, ctx.repo)} [已被上游等价合入，跳过] {subject(sha, ctx.repo)}")
    branch = unique_branch(ctx, SYNC_PREFIX + _dt.datetime.now().strftime("%Y%m%d"))
    if ns.dry_run:
        info(f"[dry-run] 计划：从 {ctx.upstream} 新建 {branch}（/tmp 临时工作树），按上序 cherry-pick；")
        info(f"[dry-run] 运行 {ctx.test_target}" + ("及全量测试" if ns.full_tests else "") + "；")
        info(f"[dry-run] 通过后在 {ctx.wt} 执行 git checkout -B {ctx.current} {branch}。")
        try:
            safety_check(ctx, ns.force)
            info("[dry-run] 安全检查通过。")
        except Abort as exc:
            warn(f"[dry-run] 实际执行时会被拒绝：{exc}")
        return EXIT_OK

    tmp_dir = tempfile.mkdtemp(prefix="iqa-sync-", dir="/tmp")
    tmp_wt = str(Path(tmp_dir, "wt"))
    logdir = Path(tmp_dir, "logs")
    logdir.mkdir()
    git(["worktree", "add", "--no-track", "-b", branch, tmp_wt, upstream_sha], ctx.repo)
    unset_upstream(branch, ctx.repo)
    info(f"已创建同步分支 {branch}，临时工作树 {tmp_wt}")

    def discard(delete_branch: bool) -> None:
        git(["worktree", "remove", "--force", tmp_wt], ctx.repo, check=False)
        git(["worktree", "prune"], ctx.repo, check=False)
        if delete_branch:
            git(["branch", "-D", branch], ctx.repo, check=False)
        shutil.rmtree(tmp_dir, ignore_errors=True)

    skipped = []
    for sha in commits:
        label = f"{short(sha, ctx.repo)} {subject(sha, ctx.repo)}"
        proc = git(["cherry-pick", sha], tmp_wt, check=False)
        if proc.returncode == 0:
            info(f"  已应用 {label}")
            continue
        conflicted = out(["diff", "--name-only", "--diff-filter=U"], tmp_wt).splitlines()
        in_progress = ref_exists("CHERRY_PICK_HEAD", tmp_wt)
        if not conflicted and in_progress:
            # 补丁已在上游（内容等价但 patch-id 不同）：应用后为空提交，跳过。
            git(["cherry-pick", "--skip"], tmp_wt)
            skipped.append(label)
            info(f"  跳过（已被上游合入，应用后为空）{label}")
            continue
        git(["cherry-pick", "--abort"], tmp_wt, check=False)
        discard(delete_branch=True)
        info(f"[冲突] cherry-pick {label} 失败，{ctx.current} 未改动；同步分支与临时工作树已清理。")
        for path in conflicted:
            info(f"  冲突文件：{path}")
        if not conflicted:
            info(f"  git 输出：{(proc.stderr or proc.stdout).strip()[:500]}")
        info("处理建议：见 README「冲突处理」——手工在临时分支解决后再移动 eval/current。")
        return EXIT_CONFLICT

    new_head = out(["rev-parse", "HEAD"], tmp_wt)
    if not run_tests(ctx, tmp_wt, ns.full_tests, logdir, ns.strict_local):
        kept_logs = Path(tempfile.mkdtemp(prefix="iqa-sync-logs-", dir="/tmp"))
        for log in logdir.iterdir():
            shutil.copy2(log, kept_logs / log.name)
        discard(delete_branch=False)
        info(f"[测试失败] 已保留同步分支 {branch}（{short(new_head, ctx.repo)}）供排查，日志在 {kept_logs}；"
             f"{ctx.current} 未改动。")
        return EXIT_TESTS

    try:
        safety_check(ctx, ns.force)
    except Abort:
        discard(delete_branch=False)
        info(f"同步分支 {branch}（{short(new_head, ctx.repo)}）已保留；处理后可用 rollback {branch} 切换。")
        raise
    discard(delete_branch=False)  # 先移除临时工作树，分支才能在评测工作树检出
    backup_current_if_orphan(ctx)
    move_current(ctx, branch)
    unset_upstream(branch, ctx.repo)
    info("")
    info("同步完成：")
    info(f"  {ctx.current}：{short(old_current, ctx.repo)} → {short(new_head, ctx.repo)}（基于 {short(upstream_sha, ctx.repo)}）")
    info(f"  同步分支 {branch} 已保留，可用于回滚。")
    if skipped:
        info(f"  跳过 {len(skipped)} 个已被上游合入的提交：" + "；".join(skipped))
    info("提醒：从评测工作树启动的服务需要重启才能生效；上游代码已变化，新结果与此前评测未必可比。")
    return EXIT_OK


SECRET_RULES = [
    ("sk- 形式密钥", re.compile(r"(?<![0-9A-Za-z])sk-[A-Za-z0-9_\-]{8,}")),
    ("本机中转端口 4002", re.compile(r"(?<![0-9A-Za-z.])4002(?![0-9A-Za-z])")),
    ("YAML api_key 配置行", re.compile(r"^[+\- ]\s*#?\s*api_key\s*:", re.MULTILINE)),
    ("长字面量 api_key", re.compile(r"api_key\s*[:=]\s*[\"']?[A-Za-z0-9_\-]{16,}")),
]


def known_secrets(ctx: Ctx, local_commits: list[str]) -> list[str]:
    """从仅本地提交的 diff 中取出 api_key 值（含注释掉的旧值），用于精确扫描；不打印。"""
    values = []
    for sha in local_commits:
        diff = out(["show", "--format=", sha], ctx.repo)
        for match in re.finditer(r"api_key\s*:\s*[\"']?([^\s\"']{8,})", diff):
            values.append(match.group(1))
    return values


def cmd_export_patch(ctx: Ctx, ns: argparse.Namespace) -> int:
    commits = ctx.our_commits()
    exportable = [sha for sha in commits if not ctx.is_local(sha)]
    local = [sha for sha in commits if ctx.is_local(sha)]
    if not exportable:
        info("没有可导出的（不带标记的）提交。")
        return EXIT_OK
    if any(ctx.is_local(sha) for sha in exportable):  # 双重保险
        raise Abort("内部错误：待导出提交中含本地标记。")
    first_local = next((i for i, sha in enumerate(commits) if ctx.is_local(sha)), None)
    if first_local is not None and any(commits.index(sha) > first_local for sha in exportable):
        warn("有可导出提交排在仅本地提交之后，补丁单独应用到上游时可能依赖本地提交。")
    stamp = _dt.datetime.now().strftime("%Y%m%d")
    outdir = Path(ns.out) if ns.out else Path(PATCH_ROOT, f"insurance_passthrough_{stamp}")
    if outdir.exists() and any(outdir.iterdir()):
        if ns.out:
            raise Abort(f"输出目录非空：{outdir}")
        outdir = Path(PATCH_ROOT, f"insurance_passthrough_{stamp}-{_dt.datetime.now().strftime('%H%M%S')}")
    outdir.mkdir(parents=True, exist_ok=True)
    files = []
    for number, sha in enumerate(exportable, start=1):
        name = out(["format-patch", "--no-signature", "--start-number", str(number), "-o", str(outdir),
                    "-1", sha], ctx.repo)
        files.append(Path(name.splitlines()[-1]))
    secrets = known_secrets(ctx, local)
    findings = []
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        for rule, pattern in SECRET_RULES:
            for match in pattern.finditer(text):
                line_no = text.count("\n", 0, match.start()) + 1
                findings.append(f"{path.name}:{line_no} 命中规则「{rule}」")
        for value in secrets:
            if value in text:
                findings.append(f"{path.name} 含本地配置中的 api_key 值（{value[:4]}****）")
    if findings:
        shutil.rmtree(outdir, ignore_errors=True)
        for item in findings:
            info(f"  {item}")
        raise Abort("敏感信息扫描未通过，已删除生成的补丁目录。")
    info(f"已导出 {len(files)} 个补丁到 {outdir}（跳过 {len(local)} 个仅本地提交），敏感信息扫描通过：")
    for sha, path in zip(exportable, files):
        info(f"  {short(sha, ctx.repo)} → {path.name}")
    info("仅供交给上游维护者评审；本工具不会推送任何内容。")
    return EXIT_OK


def cmd_rollback(ctx: Ctx, ns: argparse.Namespace) -> int:
    current_sha = out(["rev-parse", ctx.current], ctx.repo) if ref_exists(ctx.current, ctx.repo) else ""
    rows = out(["for-each-ref", "--sort=-committerdate",
                "--format=%(objectname) %(refname:short) %(committerdate:iso) %(subject)",
                f"refs/heads/{SYNC_PREFIX}*"], ctx.repo).splitlines()
    if not ns.branch:
        if not rows:
            info("没有 eval/sync-* 分支。")
            return EXIT_OK
        info(f"eval/sync-* 分支（新→旧；* 表示与 {ctx.current} 相同）：")
        for row in rows:
            sha, rest = row.split(" ", 1)
            info(f"  {'*' if sha == current_sha else ' '} {sha[:8]} {rest}")
        return EXIT_OK
    if not branch_exists(ns.branch, ctx.repo):
        raise Abort(f"分支不存在：{ns.branch}")
    if not ns.branch.startswith(SYNC_PREFIX):
        warn(f"{ns.branch} 不是 {SYNC_PREFIX}* 分支，请确认这是想要的目标。")
    target = out(["rev-parse", ns.branch], ctx.repo)
    if target == current_sha:
        info(f"{ctx.current} 已指向 {ns.branch}（{short(target, ctx.repo)}），无需回滚。")
        return EXIT_OK
    safety_check(ctx, ns.force)
    backup_current_if_orphan(ctx)
    move_current(ctx, ns.branch)
    info(f"{ctx.current}：{short(current_sha, ctx.repo) if current_sha else '(无)'} → {short(target, ctx.repo)}（{ns.branch}）")
    info("提醒：从评测工作树启动的服务需要重启才能生效。")
    return EXIT_OK


# ---------------------------------------------------------------- CLI
def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    group = common.add_argument_group("通用配置（覆盖脚本顶部默认值）")
    group.add_argument("--repo", default=REPO, help=f"主仓库路径（默认 {REPO}）")
    group.add_argument("--wt", default=EVAL_WT, help=f"评测工作树路径（默认 {EVAL_WT}）")
    group.add_argument("--upstream", default=UPSTREAM, help=f"上游引用（默认 {UPSTREAM}）")
    group.add_argument("--current", default=CURRENT, help=f"评测稳定分支（默认 {CURRENT}）")
    group.add_argument("--marker", default=LOCAL_MARKER, help=f"仅本地提交的 subject 前缀（默认 {LOCAL_MARKER}）")
    group.add_argument("--python", default=TEST_PYTHON, help="运行测试的解释器（默认主仓库 .feval_venv）")
    group.add_argument("--test-target", default=TEST_TARGET, help=f"透传测试路径（默认 {TEST_TARGET}）")

    # 通用参数只挂在子命令上（放在子命令之后），避免子命令默认值覆盖主命令上的同名参数。
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", metavar="子命令")
    sub.required = True
    p = sub.add_parser("status", parents=[common], help="查看分支与工作树状态")
    p.add_argument("--fetch", action="store_true", help="先 git fetch（只读）")
    p = sub.add_parser("sync", parents=[common], help="把我方提交重放到最新上游",
                       description="在 /tmp 临时工作树中基于上游新建 eval/sync-日期 分支并 cherry-pick 我方提交；"
                                   "冲突则清理并退出（码 2），测试失败保留分支并退出（码 3），"
                                   "成功才移动 CURRENT。")
    p.add_argument("--no-fetch", action="store_true", help="不 fetch，使用本地已有的上游引用")
    p.add_argument("--full-tests", action="store_true",
                   help="另跑全量测试；失败用例先在纯上游复跑（同样失败=环境性存量），"
                        "再在“上游+可合入提交”上复跑（不失败=仅本地配置引起，默认只警告）")
    p.add_argument("--strict-local", action="store_true",
                   help="配合 --full-tests：仅本地提交引起的失败也视为不通过（默认只警告）")
    p.add_argument("--dry-run", action="store_true", help="只打印计划，不做任何改动（也不 fetch）")
    p.add_argument("--force", action="store_true", help="评测工作树有改动或有进程运行时仍移动 CURRENT")
    p = sub.add_parser("export-patch", parents=[common], help="导出可合入上游的提交为补丁")
    p.add_argument("--out", help=f"输出目录（默认 {PATCH_ROOT}/insurance_passthrough_<日期>/）")
    p = sub.add_parser("rollback", parents=[common], help="列出 eval/sync-* 或把 CURRENT 指回某分支")
    p.add_argument("branch", nargs="?", help="回滚目标分支；省略则仅列出")
    p.add_argument("--force", action="store_true", help="越过工作树/进程安全检查")
    return parser


def main(argv: list[str] | None = None) -> int:
    ns = build_parser().parse_args(argv)
    handlers = {"status": cmd_status, "sync": cmd_sync, "export-patch": cmd_export_patch, "rollback": cmd_rollback}
    try:
        return handlers[ns.command](Ctx(ns), ns)
    except Abort as exc:
        print(f"[中止] {exc}", file=sys.stderr, flush=True)
        return exc.code
    except KeyboardInterrupt:
        print("[中止] 用户中断。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
