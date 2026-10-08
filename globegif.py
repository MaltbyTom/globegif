import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import cartopy.crs as ccrs
import numpy as np
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import threading
import queue
from PIL import Image, ImageTk
import os

class GlobeGifApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Globe GIF Generator Pro")
        self.root.minsize(700,400)
        self.root.resizable(False, False)

        # --- Variables ---
        self.input_path = tk.StringVar()
        self.output_path = tk.StringVar(value="rotating_globe.gif")
        
        self.step_var = tk.IntVar(value=5)
        self.fps_var = tk.IntVar(value=15)
        self.tilt_var = tk.IntVar(value=20)
        self.ecliptic_tilt_var = tk.IntVar(value=23)  
        self.size_var = tk.IntVar(value=400)
        
        self.show_grid_var = tk.BooleanVar(value=True)
        self.grid_step_var = tk.IntVar(value=30)
        self.lighting_var = tk.BooleanVar(value=True)
        self.transparent_var = tk.BooleanVar(value=False)
        self.output_format_var = tk.StringVar(value="GIF")
        
        self.is_rendering = False
        self.preview_photo = None

        # Worker thread -> UI thread messages (Tkinter is not thread-safe)
        self.ui_queue = queue.Queue()

        self.setup_ui()
        self._poll_ui_queue()

    def setup_ui(self):
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.pack(fill=tk.BOTH, expand=True)

        # --- LEFT COLUMN (Files & Preview) ---
        left_panel = ttk.Frame(main_frame)
        left_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 10))

        io_frame = ttk.LabelFrame(left_panel, text="File Configuration", padding="10")
        io_frame.pack(fill=tk.X, pady=(0, 10))

        ttk.Button(io_frame, text="Select Input Map (2:1)", command=self.browse_input).pack(fill=tk.X, pady=2)
        ttk.Label(io_frame, textvariable=self.input_path, wraplength=300).pack(fill=tk.X, pady=2)
        
        ttk.Button(io_frame, text="Set Output File", command=self.browse_output).pack(fill=tk.X, pady=2)
        ttk.Label(io_frame, textvariable=self.output_path, wraplength=300).pack(fill=tk.X, pady=2)

        preview_frame = ttk.LabelFrame(left_panel, text="Image Preview", padding="10")
        preview_frame.pack(fill=tk.BOTH, expand=True)
        
        self.preview_label = ttk.Label(preview_frame, text="No image selected.\nMust be 2:1 ratio.", justify="center")
        self.preview_label.pack(expand=True)

        # --- RIGHT COLUMN (Settings) ---
        right_panel = ttk.Frame(main_frame)
        right_panel.grid(row=0, column=1, sticky="nsew")

        cam_frame = ttk.LabelFrame(right_panel, text="Camera & Animation", padding="10")
        cam_frame.pack(fill=tk.X, pady=(0, 10))

        self.add_spinbox_row(cam_frame, "Degrees/Frame (Divisor of 360):", self.step_var, 1, 60, 0, values=[1,2,3,4,5,6,8,9,10,12,15,18,20,24,30,36,45,60])
        self.add_spinbox_row(cam_frame, "Frames Per Second:", self.fps_var, 1, 60, 1)
        self.add_spinbox_row(cam_frame, "Camera Tilt (Latitude -90 to 90):", self.tilt_var, -90, 90, 2)
        self.add_spinbox_row(cam_frame, "Axial Tilt (Ecliptic -90 to 90):", self.ecliptic_tilt_var, -90, 90, 3)
        self.add_spinbox_row(cam_frame, "Output Size (px):", self.size_var, 100, 2000, 4, increment=100)

        aes_frame = ttk.LabelFrame(right_panel, text="Aesthetics & Output", padding="10")
        aes_frame.pack(fill=tk.X)

        ttk.Checkbutton(aes_frame, text="Enable 3D Directional Lighting", variable=self.lighting_var).grid(row=0, column=0, columnspan=2, sticky=tk.W, pady=2)
        ttk.Checkbutton(aes_frame, text="Transparent Background", variable=self.transparent_var).grid(row=1, column=0, columnspan=2, sticky=tk.W, pady=2)
        
        ttk.Checkbutton(aes_frame, text="Show Lat/Long Grid", variable=self.show_grid_var).grid(row=2, column=0, sticky=tk.W, pady=2)
        grid_combo = ttk.Combobox(aes_frame, textvariable=self.grid_step_var, values=[15, 30], state="readonly", width=5)
        grid_combo.grid(row=2, column=1, sticky=tk.W, padx=5, pady=2)
        ttk.Label(aes_frame, text="° intervals").grid(row=2, column=2, sticky=tk.W)
        
        ttk.Label(aes_frame, text="Output Format:").grid(row=3, column=0, sticky=tk.W, pady=2)
        format_combo = ttk.Combobox(aes_frame, textvariable=self.output_format_var, values=["GIF", "WebP"], state="readonly", width=8)
        format_combo.grid(row=3, column=1, sticky=tk.W, padx=5, pady=2)
        format_combo.bind("<<ComboboxSelected>>", self.update_output_extension)

        # --- BOTTOM SPAN (Progress & Action) ---
        bottom_frame = ttk.Frame(main_frame)
        bottom_frame.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(15, 0))

        self.progress = ttk.Progressbar(bottom_frame, orient=tk.HORIZONTAL, mode='determinate')
        self.progress.pack(fill=tk.X, pady=5)
        
        self.status_var = tk.StringVar(value="Waiting for input...")
        ttk.Label(bottom_frame, textvariable=self.status_var).pack()

        self.generate_btn = ttk.Button(bottom_frame, text="Generate Rotating Globe", command=self.start_generation)
        self.generate_btn.pack(pady=10)

    def _post(self, func, *args):
        """Called from the worker thread: schedule func(*args) on the UI thread."""
        self.ui_queue.put((func, args))

    def _poll_ui_queue(self):
        """Runs on the UI thread: drain pending worker messages."""
        try:
            while True:
                func, args = self.ui_queue.get_nowait()
                func(*args)
        except queue.Empty:
            pass
        self.root.after(50, self._poll_ui_queue)

    def update_output_extension(self, event=None):
        current_path = self.output_path.get()
        if current_path:
            base, ext = os.path.splitext(current_path)
            new_ext = self.output_format_var.get().lower()
            self.output_path.set(f"{base}.{new_ext}")

    def add_spinbox_row(self, parent, label, var, vmin, vmax, row, increment=1, values=None):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky=tk.W, pady=4)
        if values:
            ttk.Combobox(parent, textvariable=var, values=values, state="readonly", width=8).grid(row=row, column=1, sticky=tk.E, padx=5)
        else:
            ttk.Spinbox(parent, from_=vmin, to=vmax, increment=increment, textvariable=var, width=9).grid(row=row, column=1, sticky=tk.E, padx=5)

    def browse_input(self):
        file_path = filedialog.askopenfilename(filetypes=[("Image Files", ("*.png", "*.jpg", "*.jpeg", "*.webp"))])
        if file_path:
            try:
                img = Image.open(file_path).convert("RGB")
                width, height = img.size
                
                ratio = width / height
                if abs(ratio - 2.0) > 0.05:
                    messagebox.showwarning(
                        "Aspect Ratio Warning", 
                        f"Image is {width}x{height} (Ratio: {ratio:.2f}:1).\n\n"
                        "Equirectangular maps should be exactly 2:1. Your globe may appear distorted."
                    )
                
                self.input_path.set(file_path)
                
                img.thumbnail((320, 160))
                self.preview_photo = ImageTk.PhotoImage(img)
                self.preview_label.config(image=self.preview_photo, text="")
                self.status_var.set("Ready to render.")
                
            except Exception as e:
                messagebox.showerror("Error", f"Could not load image: {e}")

    def browse_output(self):
        format_type = self.output_format_var.get()
        ext = f".{format_type.lower()}"
        file_path = filedialog.asksaveasfilename(defaultextension=ext, filetypes=[(f"{format_type} Image", f"*{ext}")])
        if file_path:
            self.output_path.set(file_path)

    def _generate_shadow_mask(self, size):
        x = np.linspace(0, 1, size)
        y = np.linspace(0, 1, size)
        X, Y = np.meshgrid(x, y)
        
        shade = np.clip((X**2 + Y**2) - 0.3, 0, 1)
        shade_alpha = (shade * 200).astype(np.uint8) 
        
        shadow_np = np.zeros((size, size, 4), dtype=np.uint8)
        shadow_np[..., 3] = shade_alpha
        return Image.fromarray(shadow_np)

    def start_generation(self):
        if not self.input_path.get():
            messagebox.showwarning("Missing Input", "Please select an input map image.")
            return
        if self.is_rendering: return

        # Transparent frames are all held in memory as RGBA before encoding
        est_bytes = self.size_var.get() ** 2 * 4 * (360 // self.step_var.get())
        if est_bytes > 1e9:
            if not messagebox.askokcancel(
                "High Memory Use",
                f"These settings may need roughly {est_bytes / 1e9:.1f} GB of RAM while rendering.\n\n"
                "Consider a smaller output size or a larger degrees/frame value.\n\nContinue anyway?"
            ):
                return

        self.is_rendering = True
        self.generate_btn.config(state=tk.DISABLED)
        # Reset progress bar to determinate for the rendering loop
        self.progress.config(mode='determinate', value=0)

        # Read every Tk variable here on the UI thread; the worker only sees plain values
        settings = {
            "in_file": self.input_path.get(),
            "out_file": self.output_path.get(),
            "step_deg": self.step_var.get(),
            "fps": self.fps_var.get(),
            "cam_tilt": self.tilt_var.get(),
            "axial_tilt": self.ecliptic_tilt_var.get(),
            "size": self.size_var.get(),
            "show_grid": self.show_grid_var.get(),
            "grid_step": self.grid_step_var.get(),
            "apply_lighting": self.lighting_var.get(),
            "trans_bg": self.transparent_var.get(),
            "output_format": self.output_format_var.get(),
        }
        threading.Thread(target=self.render_loop, args=(settings,), daemon=True).start()

    def render_loop(self, cfg):
        try:
            in_file = cfg["in_file"]
            out_file = cfg["out_file"]
            step_deg = cfg["step_deg"]
            fps = cfg["fps"]
            cam_tilt = cfg["cam_tilt"]
            axial_tilt = cfg["axial_tilt"]
            size = cfg["size"]

            show_grid = cfg["show_grid"]
            grid_step = cfg["grid_step"]
            apply_lighting = cfg["apply_lighting"]
            trans_bg = cfg["trans_bg"]
            output_format = cfg["output_format"]

            img = Image.open(in_file).convert("RGB")
            img_data = np.asarray(img)
            frames = []

            total_frames = 360 // step_deg
            self._post(self.progress.config, {'maximum': total_frames})
            
            shadow_mask = self._generate_shadow_mask(size) if apply_lighting else None

            for i, lon in enumerate(range(0, 360, step_deg)):
                self._post(self.status_var.set, f"Rendering Frame {i+1}/{total_frames} (Lon {lon}°)")
                
                fig = plt.figure(figsize=(size/100, size/100), dpi=100)
                fig.patch.set_alpha(0.0) 
                
                ax = plt.axes(projection=ccrs.Orthographic(central_longitude=lon, central_latitude=cam_tilt))
                ax.set_facecolor('#000000')
                ax.imshow(img_data, origin='upper', transform=ccrs.PlateCarree(), extent=[-180, 180, -90, 90], interpolation='nearest')
                
                if show_grid:
                    gl = ax.gridlines(crs=ccrs.PlateCarree(), color='black', alpha=0.4, linestyle='--')
                    gl.xlocator = mticker.FixedLocator(np.arange(-180, 181, grid_step))
                    gl.ylocator = mticker.FixedLocator(np.arange(-90, 91, grid_step))
                
                ax.axis('off')
                plt.subplots_adjust(left=0, right=1, bottom=0, top=1)
                
                fig.canvas.draw()
                rgba = np.asarray(fig.canvas.buffer_rgba())
                frame = Image.fromarray(rgba).convert('RGBA')
                
                fig.clf()
                plt.close(fig)

                if axial_tilt != 0:
                    frame = frame.rotate(-axial_tilt, resample=Image.BICUBIC, expand=False)
                
                if apply_lighting:
                    globe_alpha = frame.split()[3]
                    shadow_layer = Image.new("RGBA", frame.size)
                    shadow_layer.paste(shadow_mask, mask=globe_alpha)
                    frame = Image.alpha_composite(frame, shadow_layer)
                
                if not trans_bg:
                    bg = Image.new("RGBA", frame.size, (15, 15, 15, 255))
                    composite = Image.alpha_composite(bg, frame).convert("RGB")
                    if output_format == "GIF":
                        composite = composite.quantize(colors=256, method=Image.MEDIANCUT, dither=Image.NONE)
                    frames.append(composite)
                else:
                    frames.append(frame)
                
                self._post(self.progress.config, {'value': i + 1})

            # Switch UI to bouncing mode during the blocking save operation
            self._post(self.status_var.set, f"Encoding {output_format}... (This takes a moment)")
            self._post(self.progress.config, {'mode': 'indeterminate'})
            self._post(self.progress.start, 10)
            
            is_webp = output_format == "WebP"
            
            frames[0].save(
                out_file,
                save_all=True,
                append_images=frames[1:],
                duration=int(1000 / fps),
                loop=0,
                optimize=False, # FORCED FALSE: Cures the GIF delta-frame streaking issue completely
                transparency=0 if (trans_bg and not is_webp) else None,
                disposal=2 if trans_bg else 0,
                method=4 if is_webp else None, # DROPPED TO 4: Vastly improves WebP encoding speed
                quality=90 if is_webp else None
            )

            # Revert UI state when done
            self._post(self.progress.stop)
            self._post(self.progress.config, {'mode': 'determinate', 'value': total_frames})
            self._post(self.status_var.set, f"Done! Saved {out_file}")
            self._post(messagebox.showinfo, "Success", "Animation generation complete!")

        except Exception as e:
            self._post(self.progress.stop)
            self._post(self.progress.config, {'mode': 'determinate'})
            self._post(self.status_var.set, "Error during generation.")
            self._post(messagebox.showerror, "Render Error", str(e))
        finally:
            self.is_rendering = False
            self._post(self.generate_btn.config, {'state': tk.NORMAL})

if __name__ == "__main__":
    root = tk.Tk()
    app = GlobeGifApp(root)
    root.mainloop()