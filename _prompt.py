"""Coloured interactive prompt for run.bat. Writes the chosen flags to .last_args."""
import sys

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.text import Text
    from rich.align import Align
    from rich.prompt import IntPrompt
    HAS_RICH = True
except ImportError:
    HAS_RICH = False


APP_NAME = "GPT Promo Grabber"
AUTHOR_HANDLE = "@putrm"
CONTACT_URL = "https://t.me/putrm"


def main():
    if HAS_RICH:
        console = Console()
        title = Text()
        title.append(APP_NAME, style="bold cyan")
        title.append("\n")
        title.append("Pure HTTP + Multi-thread", style="dim italic")
        title.append("\n")
        title.append(f"by {AUTHOR_HANDLE}  ", style="bold white")
        title.append(f"|  buy: {CONTACT_URL}", style="bold green")
        console.print()
        console.print(Panel(Align.center(title), border_style="cyan", padding=(0, 2)))
        console.print()
        runs = IntPrompt.ask("[cyan]Codes to grab[/cyan]", default=1, console=console)
        workers = IntPrompt.ask("[cyan]Parallel workers[/cyan]", default=1, console=console)
    else:
        print()
        print(f"=== {APP_NAME} ===")
        print(f"by {AUTHOR_HANDLE}  |  buy: {CONTACT_URL}")
        print()
        runs_raw = input("Codes to grab [1]: ").strip() or "1"
        workers_raw = input("Parallel workers [1]: ").strip() or "1"
        try:
            runs = int(runs_raw)
            workers = int(workers_raw)
        except ValueError:
            print("Not a number.")
            sys.exit(1)

    runs = max(1, runs)
    workers = max(1, workers)

    with open(".last_args", "w", encoding="utf-8") as f:
        f.write(f"-n {runs} -w {workers}")


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        sys.exit(130)
