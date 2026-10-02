from tkinter import filedialog, messagebox, ttk
from os import getcwd, system
from pathlib import Path
from mc3ds.nbt import NBT
import tkinter as tk
import threading
import traceback
import queue
import re
import sys

BLANK_WORLD_PATH = Path(__file__).resolve().parent / "mc3ds" / "worlds" / "blankWorld"
print(BLANK_WORLD_PATH)
DEP_LIST = ["dissect.cstruct", "anvil-new", "click", "nbtlib", "p_tqdm"]
BACKGROUND = "black"
FOREGROUND = "white"


def getWorldName(byteData: bytes):
    return str(NBT(byteData).get("LevelName"))


def safeFolderName(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .") or "world"


def checkDependencies() -> bool:
    try:
        import dissect.cstruct, click, nbtlib, p_tqdm, anvil
    except ImportError:
        answer = messagebox.askyesno("Module Notice", "3DS-Chunker needs some modules to opperate.\nMay it install them now?")
        if answer:
            for module in DEP_LIST:
                system(f'"{sys.executable}" -m pip install {module}')
        else:
            sys.exit(1)
    return True


class QueueProgress:
    "forwards progress from the conversion thread to the GUI thread"

    def __init__(self, events: queue.Queue) -> None:
        self._events = events

    def status(self, message: str) -> None:
        self._events.put(("status", message))

    def warning(self, message: str) -> None:
        self._events.put(("warning", message))

    def update(self, done: int, total: int) -> None:
        self._events.put(("progress", done, total))


class ChunkerApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.events = queue.Queue()
        self.worker = None
        self.warnings = 0
        root.title("MC3DS Chunker - GUI")
        root.geometry("760x640")
        root.minsize(760, 560)
        root.config(background=BACKGROUND)

        self.ds_world = tk.StringVar()
        self.ds_output = tk.StringVar(value=getcwd())
        self.java_world = tk.StringVar()
        self.java_target = tk.StringVar()
        self.java_output = tk.StringVar(value=str(Path(getcwd()) / "output"))
        self.dimensions = {0: tk.BooleanVar(value=True), 1: tk.BooleanVar(value=True), 2: tk.BooleanVar(value=False)}
        self.offset_x = tk.StringVar(value="0")
        self.offset_z = tk.StringVar(value="0")

        to_java = tk.LabelFrame(root, text="MC3DS \u2192 Java", bg=BACKGROUND, fg=FOREGROUND, padx=8, pady=8)
        to_java.pack(fill=tk.X, padx=10, pady=(10, 5))
        self.pathRow(to_java, 0, "MC3DS world folder", self.ds_world)
        self.pathRow(to_java, 1, "Output folder", self.ds_output)
        self.ds_button = tk.Button(to_java, text="Convert 3DS to Java", command=self.startToJava)
        self.ds_button.grid(row=2, column=0, columnspan=3, pady=(8, 0))
        to_java.columnconfigure(1, weight=1)

        to_3ds = tk.LabelFrame(root, text="Java \u2192 MC3DS", bg=BACKGROUND, fg=FOREGROUND, padx=8, pady=8)
        to_3ds.pack(fill=tk.X, padx=10, pady=5)
        self.pathRow(to_3ds, 0, "Java world or region folder", self.java_world)
        self.pathRow(to_3ds, 1, "MC3DS world to insert into", self.java_target)
        self.pathRow(to_3ds, 2, "Output folder", self.java_output)

        options = tk.Frame(to_3ds, bg=BACKGROUND)
        options.grid(row=3, column=0, columnspan=3, sticky="w", pady=(4, 0))
        self.label(options, "Dimensions:").pack(side=tk.LEFT)
        for dimension, name in ((0, "Overworld"), (1, "Nether"), (2, "End")):
            tk.Checkbutton(
                options, text=name, variable=self.dimensions[dimension], bg=BACKGROUND, fg=FOREGROUND,
                selectcolor=BACKGROUND, activebackground=BACKGROUND, activeforeground=FOREGROUND,
            ).pack(side=tk.LEFT)
        self.label(options, "   Place at block X").pack(side=tk.LEFT)
        tk.Entry(options, textvariable=self.offset_x, width=8).pack(side=tk.LEFT, padx=2)
        self.label(options, "Z").pack(side=tk.LEFT)
        tk.Entry(options, textvariable=self.offset_z, width=8).pack(side=tk.LEFT, padx=2)
        self.label(
            to_3ds,
            "The Java chunks are inserted into a copy of the MC3DS world (for example a new world "
            "copied from the 3DS). A bare region folder uses the first checked dimension. "
            "Block X/Z is where Java block (0, 0) lands in the MC3DS world (snapped down to a chunk).",
            wraplength=720, justify=tk.LEFT,
        ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(4, 0))
        self.java_button = tk.Button(to_3ds, text="Convert Java to 3DS", command=self.startTo3DS)
        self.java_button.grid(row=5, column=0, columnspan=3, pady=(8, 0))
        to_3ds.columnconfigure(1, weight=1)

        status_frame = tk.Frame(root, bg=BACKGROUND)
        status_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(5, 10))
        self.progress = ttk.Progressbar(status_frame, mode="determinate", maximum=1)
        self.progress.pack(fill=tk.X)
        self.status = self.label(status_frame, "Ready", anchor="w")
        self.status.pack(fill=tk.X, pady=(4, 4))
        log_frame = tk.Frame(status_frame, bg=BACKGROUND)
        log_frame.pack(fill=tk.BOTH, expand=True)
        self.log = tk.Text(log_frame, height=12, bg=BACKGROUND, fg=FOREGROUND, insertbackground=FOREGROUND, state=tk.DISABLED, wrap=tk.WORD)
        scrollbar = tk.Scrollbar(log_frame, command=self.log.yview)
        self.log.config(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.log.tag_config("warning", foreground="orange")
        self.log.tag_config("error", foreground="red")
        self.log.tag_config("done", foreground="lime")

        self._poll = self.root.after(100, self.pollEvents)

    def close(self) -> None:
        self.root.after_cancel(self._poll)
        self.root.destroy()

    def label(self, parent, text, **kwargs) -> tk.Label:
        return tk.Label(parent, text=text, bg=BACKGROUND, fg=FOREGROUND, **kwargs)

    def pathRow(self, parent, row: int, text: str, variable: tk.StringVar) -> None:
        self.label(parent, text).grid(row=row, column=0, sticky="w", padx=(0, 8))
        tk.Entry(parent, textvariable=variable, width=70).grid(row=row, column=1, sticky="we", pady=2)
        tk.Button(parent, text="...", border=0, command=lambda: self.pickFolder(variable)).grid(row=row, column=2, padx=5)

    def pickFolder(self, variable: tk.StringVar) -> None:
        folder_path = filedialog.askdirectory()
        if folder_path:
            variable.set(folder_path)

    def writeLog(self, message: str, tag: str | None = None) -> None:
        self.log.config(state=tk.NORMAL)
        self.log.insert(tk.END, message + "\n", tag)
        self.log.see(tk.END)
        self.log.config(state=tk.DISABLED)

    def setRunning(self, running: bool) -> None:
        state = tk.DISABLED if running else tk.NORMAL
        self.ds_button.config(state=state)
        self.java_button.config(state=state)

    def startWorker(self, title: str, job) -> None:
        if self.worker is not None and self.worker.is_alive():
            return
        checkDependencies()
        self.warnings = 0
        self.progress.config(value=0, maximum=1)
        self.log.config(state=tk.NORMAL)
        self.log.delete("1.0", tk.END)
        self.log.config(state=tk.DISABLED)
        self.writeLog(title)
        self.setRunning(True)

        def run() -> None:
            try:
                self.events.put(("done", job(QueueProgress(self.events))))
            except Exception as error:
                traceback.print_exc()
                self.events.put(("error", f"{type(error).__name__}: {error}"))

        self.worker = threading.Thread(target=run, daemon=True)
        self.worker.start()

    def pollEvents(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "status":
                    self.status.config(text=event[1])
                    self.writeLog(event[1])
                elif kind == "warning":
                    self.warnings += 1
                    self.writeLog(f"WARNING: {event[1]}", "warning")
                elif kind == "progress":
                    self.progress.config(maximum=max(event[2], 1), value=event[1])
                elif kind == "done":
                    self.setRunning(False)
                    self.status.config(text="Done")
                    self.writeLog(event[1], "done")
                    if self.warnings:
                        messagebox.showwarning("Completed with warnings", f"{event[1]}\n\n{self.warnings:d} warnings, see the log.")
                    else:
                        messagebox.showinfo("Done", event[1])
                elif kind == "error":
                    self.setRunning(False)
                    self.status.config(text="Conversion failed")
                    self.writeLog(f"ERROR: {event[1]}", "error")
                    messagebox.showerror("Conversion failed", event[1])
        except queue.Empty:
            pass
        self._poll = self.root.after(100, self.pollEvents)

    def startToJava(self) -> None:
        world_path = Path(self.ds_world.get().strip())
        output_path = Path(self.ds_output.get().strip() or getcwd())
        if not (world_path / "level.dat").is_file() or not (world_path / "db" / "cdb").is_dir():
            messagebox.showerror("Error", "Pick an MC3DS world folder (it contains level.dat and db).")
            return
        with open(world_path / "level.dat", "rb") as f:
            worldName = getWorldName(f.read())

        def job(progress: QueueProgress) -> str:
            from mc3ds.classes import World
            from mc3ds.convert import convert

            progress.status(f"Reading MC3DS world {worldName}...")
            world = World(world_path)
            world_out = output_path / safeFolderName(worldName)
            convert(world, BLANK_WORLD_PATH, world_out, progress=progress)
            return f"Converted {len(world.entries):d} chunks to the Java world {world_out}"

        self.startWorker(f"MC3DS \u2192 Java: {world_path}", job)

    def startTo3DS(self) -> None:
        java_path = Path(self.java_world.get().strip())
        target_path = Path(self.java_target.get().strip())
        output_path = Path(self.java_output.get().strip() or getcwd())
        dimensions = [dimension for dimension, checked in self.dimensions.items() if checked.get()]
        if not self.java_world.get().strip() or not java_path.is_dir():
            messagebox.showerror("Error", "Pick a Java world folder or a region folder.")
            return
        if not self.java_target.get().strip() or not target_path.is_dir():
            messagebox.showerror("Error", "Pick the MC3DS world to insert the chunks into.")
            return
        if not dimensions:
            messagebox.showerror("Error", "Check at least one dimension.")
            return
        try:
            block_x = int(self.offset_x.get().strip() or "0")
            block_z = int(self.offset_z.get().strip() or "0")
        except ValueError:
            messagebox.showerror("Error", "Block X/Z must be whole numbers (Java/MC3DS block coords).")
            return
        # Converter places by chunk; snap toward -inf so block 0..15 → chunk 0
        start_chunk = (block_x // 16, block_z // 16)

        def job(progress: QueueProgress) -> str:
            from mc3ds.to3ds import convert

            result = convert(
                java_path, target_path, output_path, dimensions=dimensions,
                region_dimension=dimensions[0], start_chunk=start_chunk, progress=progress,
            )
            snapped_x, snapped_z = start_chunk[0] * 16, start_chunk[1] * 16
            summary = (
                f"Converted {result.chunks:d} chunks from {result.regions:d} regions into {result.output}"
                f"\nPlaced Java (0,0) at MC3DS block ({snapped_x:d}, {snapped_z:d}) "
                f"(chunk offset {start_chunk[0]:d}, {start_chunk[1]:d})"
            )
            if (block_x, block_z) != (snapped_x, snapped_z):
                summary += (
                    f"\nNote: entered ({block_x:d}, {block_z:d}) was snapped down to the chunk corner"
                )
            if result.errors:
                summary += f"\n{result.errors:d} chunks could not be read and were written as air"
            if getattr(result, "substituted", None):
                summary += f"\n{len(result.substituted):d} missing block types remapped to similar MC3DS blocks"
            if getattr(result, "unmapped_entities", None):
                summary += f"\n{len(result.unmapped_entities):d} entity types skipped (unsupported on 3DS)"
            if getattr(result, "unmapped_items", None):
                summary += f"\n{len(result.unmapped_items):d} item types skipped in chests/entities"
            return summary

        self.startWorker(f"Java \u2192 MC3DS: {java_path}", job)


def main() -> None:
    root = tk.Tk()
    ChunkerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
