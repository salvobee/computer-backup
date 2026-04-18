#!/usr/bin/env python3
"""
Windows User Data Backup
Eseguire da una distro Linux live (USB) per fare il backup dei dati utente
di un'installazione Windows montata (NTFS via ntfs-3g/ntfs3).

Uso:
  pip install -r requirements.txt
  python3 backup.py            # wizard interattivo
  python3 backup.py --check    # verifica dipendenze
"""

import argparse
import logging
import os
import re
import shutil
import subprocess
import sys
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    from prompt_toolkit import prompt
    from prompt_toolkit.completion import PathCompleter
    from prompt_toolkit.shortcuts import (
        checkboxlist_dialog, radiolist_dialog, yes_no_dialog,
    )
    from rich.console import Console, Group
    from rich.live import Live
    from rich.panel import Panel
    from rich.progress import (
        BarColumn, DownloadColumn, Progress, TaskID,
        TextColumn, TimeRemainingColumn,
    )
    from rich.table import Table
except ImportError as e:
    print(f"Manca dipendenza: {e.name}", file=sys.stderr)
    print("Installa con: pip install -r requirements.txt", file=sys.stderr)
    sys.exit(1)


# Mappa nome logico → nome reale della cartella nel profilo Windows
WINDOWS_USER_DIRS = {
    "Documents": "Documents",
    "Desktop":   "Desktop",
    "Downloads": "Downloads",
    "Pictures":  "Pictures",
    "Videos":    "Videos",
    "Music":     "Music",
}

# File-spazzatura tipici di Windows che escludiamo sempre
EXCLUDE_PATTERNS = [
    "Thumbs.db", "desktop.ini", "~$*", "*.tmp",
    "ntuser.dat*", "NTUSER.DAT*", "NTUSER.DAT.LOG*",
    "$RECYCLE.BIN", "System Volume Information",
]

# Profili "di sistema" da nascondere nella selezione utenti
SYSTEM_PROFILES = {
    "Public", "Default", "Default User", "All Users",
    "defaultuser0", "WsiAccount", "DefaultAppPool",
}

console = Console()
logger = logging.getLogger("backup")


# ============================== Backup target ==============================

class BackupTarget(ABC):
    """Interfaccia per destinazioni backup. Oggi solo Local; in futuro S3/GDrive."""
    @abstractmethod
    def prepare(self, label: str) -> str: ...
    @abstractmethod
    def describe(self) -> str: ...
    @abstractmethod
    def free_space(self) -> Optional[int]: ...


class LocalTarget(BackupTarget):
    def __init__(self, root: Path):
        self.root = root.resolve()

    def prepare(self, label: str) -> str:
        # Sostituisce i separatori che potrebbero confondere il filesystem
        safe = label.replace(os.sep, "_")
        dest = self.root / safe
        dest.mkdir(parents=True, exist_ok=True)
        return str(dest)

    def describe(self) -> str:
        return f"Local: {self.root}"

    def free_space(self) -> Optional[int]:
        return shutil.disk_usage(self.root).free


# ============================== Discovery ==============================

def find_windows_installs() -> list[Path]:
    """Cerca mount con cartella Users/ tipica di Windows."""
    found = []
    bases = ["/mnt", "/media", "/run/media"]
    for base in bases:
        bp = Path(base)
        if not bp.exists():
            continue
        # Profondità 1, 2, 3 (es: /media/ubuntu/<UUID>/)
        for depth in (1, 2, 3):
            try:
                for p in bp.glob("/".join(["*"] * depth)):
                    try:
                        if p.is_dir() and (p / "Users").is_dir():
                            found.append(p.resolve())
                    except (PermissionError, OSError):
                        continue
            except (PermissionError, OSError):
                continue
    seen, out = set(), []
    for p in found:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def list_windows_users(install_root: Path) -> list[Path]:
    users_dir = install_root / "Users"
    if not users_dir.is_dir():
        return []
    try:
        all_dirs = sorted(
            p for p in users_dir.iterdir()
            if p.is_dir() and not p.name.startswith(".")
        )
    except PermissionError:
        return []
    filtered = [p for p in all_dirs if p.name not in SYSTEM_PROFILES]
    # Se il filtro ha azzerato tutto, mostra comunque tutto
    return filtered if filtered else all_dirs


# ============================== Wizard ==============================

@dataclass
class BackupTask:
    label: str         # nome descrittivo + sotto-path destinazione
    source: Path
    size_bytes: int = 0


@dataclass
class BackupPlan:
    tasks: list[BackupTask]
    target: BackupTarget
    workers: int
    log_path: Path


def wizard() -> BackupPlan:
    console.print(Panel.fit(
        "[bold cyan]Windows User Data Backup[/bold cyan]\n"
        "Wizard interattivo — Tab per autocompletare i path",
        border_style="cyan",
    ))

    # 1) Installazione Windows
    installs = find_windows_installs()
    if not installs:
        console.print("[yellow]Nessuna installazione Windows trovata in /mnt, /media, /run/media.[/yellow]")
        manual = prompt(
            "Path manuale alla root dell'installazione Windows (contenente Users/): ",
            completer=PathCompleter(only_directories=True, expanduser=True),
        ).strip()
        if not manual:
            sys.exit("Annullato.")
        install = Path(manual).expanduser().resolve()
        if not (install / "Users").is_dir():
            sys.exit(f"{install} non sembra una root Windows (manca Users/).")
    elif len(installs) == 1:
        install = installs[0]
        console.print(f"[green]Installazione Windows trovata:[/green] {install}")
    else:
        chosen = radiolist_dialog(
            title="Installazioni Windows trovate",
            text="Scegli quella da cui fare backup:",
            values=[(p, str(p)) for p in installs],
        ).run()
        if chosen is None:
            sys.exit("Annullato.")
        install = chosen

    # 2) Utenti
    users = list_windows_users(install)
    if not users:
        sys.exit(f"Nessun utente trovato in {install}/Users")

    selected_users = checkboxlist_dialog(
        title="Utenti Windows",
        text="Spazio per selezionare, Invio per confermare:",
        values=[(u, u.name) for u in users],
        default_values=[users[0]] if len(users) == 1 else [],
    ).run()
    if not selected_users:
        sys.exit("Nessun utente selezionato.")

    # 3) Per ogni utente: directory standard + path custom
    tasks: list[BackupTask] = []
    for user in selected_users:
        present = [(name, real) for name, real in WINDOWS_USER_DIRS.items()
                   if (user / real).is_dir()]
        if present:
            chosen_dirs = checkboxlist_dialog(
                title=f"Cartelle standard di '{user.name}'",
                text="Tutte pre-selezionate; deseleziona quelle da escludere:",
                values=[((user / real, name), name) for name, real in present],
                default_values=[(user / real, name) for name, real in present],
            ).run()
            if chosen_dirs:
                for src, name in chosen_dirs:
                    tasks.append(BackupTask(label=f"{user.name}/{name}", source=src))

        console.print(f"[cyan]Path custom per '{user.name}'[/cyan] (Invio vuoto per terminare)")
        completer = PathCompleter(expanduser=True)
        while True:
            try:
                extra = prompt("  Path: ", completer=completer).strip()
            except EOFError:
                break
            if not extra:
                break
            p = Path(extra).expanduser().resolve()
            if not p.exists():
                console.print(f"  [red]Non esiste:[/red] {p}")
                continue
            if not p.is_dir():
                console.print(f"  [red]Non è una directory:[/red] {p}")
                continue
            default_label = f"{user.name}/custom_{p.name}"
            label = prompt(f"  Label (default '{default_label}'): ").strip() or default_label
            tasks.append(BackupTask(label=label, source=p))

    if not tasks:
        sys.exit("Nessuna sorgente selezionata.")

    # 4) Destinazione
    console.print()
    dest_str = prompt(
        "Directory di destinazione del backup: ",
        completer=PathCompleter(only_directories=True, expanduser=True),
    ).strip()
    if not dest_str:
        sys.exit("Annullato.")
    dest = Path(dest_str).expanduser().resolve()
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        sys.exit(f"Impossibile creare {dest}: {e}")
    if not os.access(dest, os.W_OK):
        sys.exit(f"Destinazione non scrivibile: {dest} (servono permessi root?)")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_root = dest / f"backup_{stamp}"
    backup_root.mkdir(parents=True, exist_ok=True)
    target = LocalTarget(backup_root)

    # 5) Worker
    raw = prompt("Numero di copie parallele (default 2, max 4 consigliato su HDD): ").strip()
    try:
        workers = int(raw) if raw else 2
    except ValueError:
        workers = 2
    workers = max(1, min(8, workers))

    log_path = backup_root / f"backup_{stamp}.log"
    return BackupPlan(tasks=tasks, target=target, workers=workers, log_path=log_path)


# ============================== Size estimation ==============================

def estimate_size(path: Path) -> int:
    """du -sb: veloce, gestisce errori internamente."""
    try:
        out = subprocess.check_output(
            ["du", "-sb", "--apparent-size", str(path)],
            stderr=subprocess.DEVNULL, text=True, timeout=600,
        )
        return int(out.split()[0])
    except (subprocess.SubprocessError, ValueError, IndexError):
        return 0


# ============================== Rsync runner ==============================

# Esempio riga rsync --info=progress2:
#   "  1,234,567  45%   12.34MB/s    0:01:23 (xfr#42, to-chk=100/200)"
PROGRESS_RE = re.compile(
    r"^\s*([\d,]+)\s+(\d+)%\s+([\d.]+\S+/s)\s+([\d:]+)"
)


@dataclass
class TaskState:
    bytes_done: int = 0
    bytes_prev: int = 0
    percent: int = 0
    speed: str = ""
    rc: Optional[int] = None
    error: Optional[str] = None


def run_rsync(task: BackupTask, dest_path: str, state: TaskState,
              global_progress: Progress, global_task_id: TaskID,
              task_progress: Progress, task_id: TaskID) -> None:
    cmd = [
        "rsync",
        # -rltD = ricorsivo + symlink + tempi + device/special
        # niente -p/-o/-g: NTFS+ntfs-3g non gestisce permessi/owner Linux
        "-rltD",
        "--info=progress2",
        "--no-i-r",          # disabilita incremental recursion → percentuali stabili
        "--human-readable",
        "--partial",         # mantieni file parzialmente trasferiti per ripresa
        "--safe-links",      # ignora symlink che escono dal source tree
    ]
    for pat in EXCLUDE_PATTERNS:
        cmd.extend(["--exclude", pat])
    cmd.append(str(task.source) + "/")
    cmd.append(dest_path + "/")

    logger.info(f"START {task.label} → {dest_path}")

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            bufsize=0,
        )
    except FileNotFoundError:
        state.error = "rsync non trovato nel PATH"
        state.rc = -1
        logger.error(state.error)
        return

    # Lettura char-by-char: rsync usa \r per aggiornare la riga progress in-place
    buf = bytearray()
    assert proc.stdout is not None
    while True:
        b = proc.stdout.read(1)
        if not b:
            break
        if b in (b"\r", b"\n"):
            line = buf.decode(errors="replace").strip()
            buf.clear()
            if not line:
                continue
            m = PROGRESS_RE.match(line)
            if m:
                bytes_done = int(m.group(1).replace(",", ""))
                state.percent = int(m.group(2))
                state.speed = m.group(3)
                delta = bytes_done - state.bytes_prev
                state.bytes_prev = bytes_done
                state.bytes_done = bytes_done
                task_progress.update(
                    task_id, completed=bytes_done,
                    description=f"{task.label} [{state.speed}]",
                )
                if delta > 0:
                    global_progress.update(global_task_id, advance=delta)
            elif line.lower().startswith("rsync:") or "error" in line.lower():
                logger.warning(f"{task.label}: {line}")
        else:
            buf.extend(b)

    proc.wait()
    state.rc = proc.returncode

    err_text = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
    for line in err_text.strip().splitlines():
        if line.strip():
            logger.warning(f"{task.label} stderr: {line}")

    # rsync exit codes:
    #   0  = ok
    #   23 = trasferimento parziale (alcuni file falliti, tipicamente permessi)
    #   24 = file scomparso durante la copia
    #   altri = errore vero
    if proc.returncode == 0:
        logger.info(f"OK {task.label}")
    elif proc.returncode in (23, 24):
        logger.warning(f"PARTIAL {task.label} (rsync rc={proc.returncode})")
    else:
        state.error = f"rsync rc={proc.returncode}"
        logger.error(f"FAIL {task.label} (rc={proc.returncode}): {err_text.strip()[:500]}")

    if proc.returncode in (0, 23, 24):
        # Forza barra al 100% (la size stimata può differire dalla reale)
        target_total = max(state.bytes_done, task.size_bytes, 1)
        task_progress.update(task_id, completed=target_total, total=target_total)


# ============================== Orchestrator ==============================

def run_backup(plan: BackupPlan):
    plan.log_path.parent.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(plan.log_path, encoding="utf-8")
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logger.addHandler(fh)
    logger.setLevel(logging.INFO)
    logger.info(f"=== Backup avviato — destinazione: {plan.target.describe()}")

    # Stima dimensioni
    with console.status("[cyan]Calcolo dimensioni delle sorgenti...[/cyan]"):
        for t in plan.tasks:
            t.size_bytes = estimate_size(t.source)
            logger.info(f"SIZE {t.label}: {human_bytes(t.size_bytes)} ({t.source})")
    total_bytes = sum(t.size_bytes for t in plan.tasks)

    # Riepilogo
    tbl = Table(title="Piano di backup")
    tbl.add_column("Label"); tbl.add_column("Source"); tbl.add_column("Size", justify="right")
    for t in plan.tasks:
        tbl.add_row(t.label, str(t.source), human_bytes(t.size_bytes))
    tbl.add_row("[bold]TOTALE[/bold]", "", f"[bold]{human_bytes(total_bytes)}[/bold]")
    console.print(tbl)

    # Check spazio destinazione
    free = plan.target.free_space()
    if free is not None and free < int(total_bytes * 1.05):
        ok = yes_no_dialog(
            title="Spazio insufficiente",
            text=f"Spazio libero in destinazione: {human_bytes(free)}\n"
                 f"Stima necessaria: ~{human_bytes(total_bytes)}\n\nProseguire comunque?",
        ).run()
        if not ok:
            sys.exit("Annullato.")

    console.print(f"\n[bold]Avvio con {plan.workers} worker paralleli[/bold]")
    console.print(f"[dim]Log: {plan.log_path}[/dim]\n")

    global_progress = Progress(
        TextColumn("[bold blue]TOTALE"),
        BarColumn(bar_width=50),
        DownloadColumn(),
        TextColumn("•"),
        TimeRemainingColumn(),
    )
    task_progress = Progress(
        TextColumn("[cyan]{task.description}"),
        BarColumn(bar_width=30),
        TextColumn("{task.percentage:>5.1f}%"),
        DownloadColumn(),
    )
    global_task_id = global_progress.add_task("totale", total=max(1, total_bytes))

    states: dict[str, TaskState] = {}
    task_ids: dict[str, TaskID] = {}
    for t in plan.tasks:
        tid = task_progress.add_task(t.label, total=max(1, t.size_bytes), visible=False)
        task_ids[t.label] = tid
        states[t.label] = TaskState()

    group = Group(global_progress, task_progress)

    with Live(group, console=console, refresh_per_second=8):
        with ThreadPoolExecutor(max_workers=plan.workers) as ex:
            futures = {}
            for t in plan.tasks:
                dest_path = plan.target.prepare(t.label)
                task_progress.update(task_ids[t.label], visible=True)
                fut = ex.submit(
                    run_rsync, t, dest_path, states[t.label],
                    global_progress, global_task_id,
                    task_progress, task_ids[t.label],
                )
                futures[fut] = t

            for fut in as_completed(futures):
                t = futures[fut]
                try:
                    fut.result()
                except Exception as e:
                    logger.exception(f"FAIL {t.label}: eccezione non gestita")
                    states[t.label].error = str(e)

        # Allinea la barra globale al totale effettivo
        global_progress.update(global_task_id, completed=max(1, total_bytes))

    # Report finale
    final = Table(title="Risultato finale")
    final.add_column("Label"); final.add_column("Stato")
    final.add_column("Bytes", justify="right"); final.add_column("Note")

    ok_n = warn_n = err_n = 0
    for t in plan.tasks:
        s = states[t.label]
        if s.error:
            stato = "[red]ERROR[/red]"; err_n += 1
        elif s.rc in (23, 24):
            stato = "[yellow]PARTIAL[/yellow]"; warn_n += 1
        else:
            stato = "[green]OK[/green]"; ok_n += 1
        note = s.error or (f"rsync rc={s.rc}" if s.rc not in (0, None) else "")
        final.add_row(t.label, stato, human_bytes(s.bytes_done), note)
    console.print(final)

    summary = (f"\n[bold]OK:[/bold] {ok_n}  "
               f"[yellow]Warning:[/yellow] {warn_n}  "
               f"[red]Errori:[/red] {err_n}")
    console.print(summary)
    console.print(f"[dim]Log: {plan.log_path}[/dim]")
    logger.info(f"=== Fine — ok={ok_n} warn={warn_n} err={err_n}")


def human_bytes(n: int) -> str:
    f = float(n)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or u == "TB":
            return f"{f:.1f} {u}"
        f /= 1024
    return f"{n} B"


# ============================== main ==============================

def main():
    parser = argparse.ArgumentParser(
        description="Backup interattivo di dati utente Windows da Linux live USB.",
    )
    parser.add_argument("--check", action="store_true",
                        help="Verifica disponibilità rsync e dipendenze, poi esce.")
    args = parser.parse_args()

    if args.check:
        try:
            v = subprocess.check_output(["rsync", "--version"], text=True).splitlines()[0]
            console.print(f"[green]✓[/green] {v}")
            console.print("[green]✓[/green] prompt_toolkit, rich")
            sys.exit(0)
        except Exception as e:
            console.print(f"[red]✗ rsync mancante: {e}[/red]")
            sys.exit(1)

    try:
        plan = wizard()
    except (KeyboardInterrupt, EOFError):
        console.print("\n[yellow]Interrotto durante il wizard.[/yellow]")
        sys.exit(130)

    try:
        run_backup(plan)
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrotto. I file parziali restano in destinazione "
                      "(rsync --partial); rilancia per riprendere.[/yellow]")
        sys.exit(130)
    except Exception:
        logger.exception("Errore non gestito nel run_backup")
        console.print_exception()
        sys.exit(1)


if __name__ == "__main__":
    main()
