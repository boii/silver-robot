"""Prompt interaktif berwarna untuk run.bat. Output pilihan ke .last_args."""
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


def main():
    if HAS_RICH:
        console = Console()
        title = Text()
        title.append("BBVA ", style="bold blue")
        title.append("OpenAI ", style="bold cyan")
        title.append("Code Grabber", style="bold white")
        title.append("\n")
        title.append("Pure HTTP + Multi-thread", style="dim italic")
        console.print()
        console.print(Panel(Align.center(title), border_style="blue", padding=(0, 2)))
        console.print()
        runs = IntPrompt.ask("[cyan]Jumlah kode[/cyan]", default=1, console=console)
        workers = IntPrompt.ask("[cyan]Jumlah thread paralel[/cyan]", default=1, console=console)
    else:
        print()
        print("=== BBVA OpenAI Code Grabber ===")
        print()
        runs_raw = input("Jumlah kode [1]: ").strip() or "1"
        workers_raw = input("Jumlah thread paralel [1]: ").strip() or "1"
        try:
            runs = int(runs_raw)
            workers = int(workers_raw)
        except ValueError:
            print("Input bukan angka.")
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
