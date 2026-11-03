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
from PIL import Image, ImageTk, ImageChops
import os

# Empty margin kept beyond the outer ring edge (fraction of the outer radius)
RING_MARGIN = 1.04


# ======================================================================
#  Ring helpers (module level so they can be tested without the UI)
# ======================================================================

def load_ring_band(path):
    """Load a colour band image as an (N, 4) float32 straight-alpha RGBA array.

    Index 0 is the inner edge of the ring, index N-1 the outer edge.
    A 1px-tall (or 1px-wide) strip is ideal. If the image is thicker, it is
    averaged across its short side (alpha-weighted). Wide images are read
    left->right, tall images top->bottom.
    Returns (band, (width, height)).
    """
    img = Image.open(path).convert("RGBA")
    w, h = img.size
    arr = np.asarray(img, dtype=np.float32) / 255.0      # (h, w, 4)
    if h > w:
        arr = arr.transpose(1, 0, 2)                      # axis 0 = thickness, axis 1 = length
    alpha = arr[..., 3]
    pm = arr[..., :3] * alpha[..., None]                  # premultiply so averaging is correct
    pm_mean = pm.mean(axis=0)
    a_mean = alpha.mean(axis=0)
    safe = np.maximum(a_mean, 1e-6)[:, None]
    rgb = np.where(a_mean[:, None] > 1e-6, pm_mean / safe, 0.0)
    band = np.concatenate([rgb, a_mean[:, None]], axis=1).astype(np.float32)
    return band, (w, h)


def prepare_band(band, flip, opacity_pct):
    """Apply the flip and global-opacity settings to a loaded band."""
    b = band[::-1].copy() if flip else band.copy()
    b[:, 3] *= max(0.0, min(100.0, float(opacity_pct))) / 100.0
    return b


def band_preview_image(band, w=300, h=14):
    """Small RGB strip (over a checkerboard) showing the band, inner edge on the left."""
    idx = np.linspace(0, len(band) - 1, w).round().astype(int)
    row = band[idx]                                        # (w, 4)
    strip = np.tile(row[None, :, :], (h, 1, 1))
    check = ((np.arange(w)[None, :] // 7 + np.arange(h)[:, None] // 7) % 2).astype(np.float32)
    bg = (0.35 + 0.2 * check)[..., None]
    a = strip[..., 3:4]
    rgb = strip[..., :3] * a + bg * (1.0 - a)
    return Image.fromarray((np.clip(rgb, 0, 1) * 255 + 0.5).astype(np.uint8))


def build_ring_layers(band, size, r_in, r_out, planet_frac, cam_tilt, axial_tilt,
                      ss=None, block=32):
    """Build the two static ring layers as RGBA PIL images (size x size).

    whole : the complete ring (composited UNDER the planet, so the planet hides
            the far half automatically)
    near  : only the half of the ring NEAR the camera (composited OVER the planet)

    Geometry: the ring lies in the planet's equatorial plane. Viewed from
    latitude `cam_tilt` it projects to an ellipse with minor/major ratio
    |sin(cam_tilt)|. The axial tilt is a 2D roll of the whole frame, so each
    screen pixel is rotated back into the un-tilted frame before it is mapped
    to ring-plane coordinates. Each pixel is supersampled (ss x ss) for smooth
    edges and the band is interpolated in premultiplied-alpha space.
    Rings are identical in every frame (the band is radially symmetric), so
    this runs once per render.
    """
    if ss is None:
        ss = 3 if size <= 1000 else 2
    band = np.asarray(band, dtype=np.float32)
    pm = band.copy()
    pm[:, :3] *= pm[:, 3:4]
    grid = np.linspace(0.0, 1.0, len(band))

    S = size * ss
    ppr = planet_frac * size / 2.0                         # planet radius, in output pixels
    ang = np.radians(axial_tilt)
    ca, sa = float(np.cos(ang)), float(np.sin(ang))
    s = max(abs(float(np.sin(np.radians(cam_tilt)))), 0.02)   # clamp: edge-on view -> thin line
    far_is_up = cam_tilt >= 0     # camera above the ring plane -> far side appears higher on screen
    span = r_out - r_in

    k = (np.arange(S, dtype=np.float32) + 0.5) / ss        # sample centres, in output-pixel units
    u = (k - size / 2.0) / ppr                             # screen x, in planet radii
    v = (size / 2.0 - k) / ppr                             # screen y (up), in planet radii

    whole = np.zeros((size, size, 4), dtype=np.float32)
    near = np.zeros((size, size, 4), dtype=np.float32)

    rows_per = block * ss
    for r0 in range(0, S, rows_per):
        vv = v[r0:r0 + rows_per, None]                     # (R, 1)
        xx = u[None, :]                                    # (1, S)
        xu = xx * ca - vv * sa                             # un-tilted coordinates
        yu = xx * sa + vv * ca
        t = (np.hypot(xu, yu / s) - r_in) / span           # 0 at inner edge, 1 at outer edge
        inside = (t >= 0.0) & (t <= 1.0)
        tc = np.clip(t, 0.0, 1.0)

        strip = np.empty(t.shape + (4,), dtype=np.float32)
        for ch in range(4):
            strip[..., ch] = np.interp(tc, grid, pm[:, ch])
        strip *= inside[..., None]

        near_mask = (yu < 0) if far_is_up else (yu > 0)
        R = strip.shape[0]
        o0 = r0 // ss
        o1 = o0 + R // ss
        whole[o0:o1] = strip.reshape(R // ss, ss, size, ss, 4).mean(axis=(1, 3))
        part = strip * near_mask[..., None]
        near[o0:o1] = part.reshape(R // ss, ss, size, ss, 4).mean(axis=(1, 3))

    return _premult_to_pil(whole), _premult_to_pil(near)


def _premult_to_pil(pm):
    a = pm[..., 3:4]
    rgb = np.where(a > 1e-6, pm[..., :3] / np.maximum(a, 1e-6), 0.0)
    out = np.concatenate([np.clip(rgb, 0.0, 1.0), np.clip(a, 0.0, 1.0)], axis=-1)
    return Image.fromarray((out * 255.0 + 0.5).astype(np.uint8))


# ======================================================================
#  Rendering (module level: one frame at a time, no UI access)
# ======================================================================

def generate_shadow_mask(size, planet_frac=1.0):
    """Fixed screen-space shading, anchored to the planet disc (not the whole frame)."""
    x = (np.linspace(0, 1, size) - 0.5) / planet_frac + 0.5
    y = (np.linspace(0, 1, size) - 0.5) / planet_frac + 0.5
    X, Y = np.meshgrid(x, y)

    shade = np.clip((X**2 + Y**2) - 0.3, 0, 1)
    shade_alpha = (shade * 200).astype(np.uint8)

    shadow_np = np.zeros((size, size, 4), dtype=np.uint8)
    shadow_np[..., 3] = shade_alpha
    return Image.fromarray(shadow_np)


def prepare_render(cfg, status=None):
    """Everything that is the same for every frame: map, lighting mask, ring layers."""
    img = Image.open(cfg["in_file"]).convert("RGB")
    ctx = {
        "img_data": np.asarray(img),
        "shadow_mask": generate_shadow_mask(cfg["size"], cfg["planet_frac"]) if cfg["apply_lighting"] else None,
        "ring_whole": None,
        "ring_near": None,
    }
    if cfg["ring_enabled"]:
        if status:
            status("Building rings...")
        band = prepare_band(cfg["ring_band"], cfg["ring_flip"], cfg["ring_opacity"])
        ctx["ring_whole"], ctx["ring_near"] = build_ring_layers(
            band, cfg["size"], cfg["ring_inner"], cfg["ring_outer"],
            cfg["planet_frac"], cfg["cam_tilt"], cfg["axial_tilt"])
    return ctx


def render_frame(cfg, ctx, lon):
    """Render one RGBA frame at central longitude `lon`: back ring, planet, front ring."""
    size = cfg["size"]
    pf = cfg["planet_frac"]

    fig = plt.figure(figsize=(size / 100, size / 100), dpi=100)
    fig.patch.set_alpha(0.0)

    m = (1.0 - pf) / 2.0   # centre the (possibly shrunken) globe to leave room for rings
    ax = fig.add_axes([m, m, pf, pf],
                      projection=ccrs.Orthographic(central_longitude=lon, central_latitude=cfg["cam_tilt"]))
    ax.set_facecolor('#000000')
    ax.imshow(ctx["img_data"], origin='upper', transform=ccrs.PlateCarree(),
              extent=[-180, 180, -90, 90], interpolation='nearest')

    if cfg["show_grid"]:
        gl = ax.gridlines(crs=ccrs.PlateCarree(), color='black', alpha=0.4, linestyle='--')
        gl.xlocator = mticker.FixedLocator(np.arange(-180, 181, cfg["grid_step"]))
        gl.ylocator = mticker.FixedLocator(np.arange(-90, 91, cfg["grid_step"]))

    ax.axis('off')

    fig.canvas.draw()
    rgba = np.asarray(fig.canvas.buffer_rgba())
    frame = Image.fromarray(rgba).convert('RGBA')

    fig.clf()
    plt.close(fig)

    if cfg["axial_tilt"] != 0:
        frame = frame.rotate(-cfg["axial_tilt"], resample=Image.BICUBIC, expand=False)

    if cfg["apply_lighting"]:
        globe_alpha = frame.split()[3]
        shadow_layer = Image.new("RGBA", frame.size)
        shadow_layer.paste(ctx["shadow_mask"], mask=globe_alpha)
        frame = Image.alpha_composite(frame, shadow_layer)

    if ctx["ring_whole"] is not None:
        # Whole ring goes under the planet. The near half goes over it, but only
        # where the planet actually is (its alpha), so the two never double up
        # in open space and no seam appears where the halves meet.
        planet_alpha = frame.split()[3]
        near = ctx["ring_near"].copy()
        near.putalpha(ImageChops.multiply(near.split()[3], planet_alpha))
        frame = Image.alpha_composite(ctx["ring_whole"], frame)
        frame = Image.alpha_composite(frame, near)

    return frame


def checker_background(size, cell=12):
    """Grey checkerboard used to show transparency in the preview window."""
    idx = np.arange(size)
    check = ((idx[None, :] // cell + idx[:, None] // cell) % 2).astype(np.uint8)
    gray = (90 + 40 * check).astype(np.uint8)
    rgba = np.stack([gray, gray, gray, np.full_like(gray, 255)], axis=-1)
    return Image.fromarray(rgba)


# ======================================================================
#  UI
# ======================================================================

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

        # --- Ring variables ---
        self.ring_enabled_var = tk.BooleanVar(value=False)
        self.ring_path = tk.StringVar()
        self.ring_inner_var = tk.DoubleVar(value=1.25)   # distance from planet, in planet radii
        self.ring_width_var = tk.DoubleVar(value=1.0)    # ring width, in planet radii
        self.ring_opacity_var = tk.IntVar(value=100)
        self.ring_flip_var = tk.BooleanVar(value=False)
        self.ring_info_var = tk.StringVar()
        self.ring_band = None
        self.ring_strip_photo = None
        
        self.is_rendering = False
        self.preview_photo = None
        self.preview_win = None
        self.preview_win_label = None
        self.preview_win_photo = None

        # Worker thread -> UI thread messages (Tkinter is not thread-safe)
        self.ui_queue = queue.Queue()

        self.setup_ui()
        self.update_ring_info()
        self.ring_inner_var.trace_add("write", self.update_ring_info)
        self.ring_width_var.trace_add("write", self.update_ring_info)
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
        self.aes_frame = aes_frame

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

        ttk.Checkbutton(aes_frame, text="Add Planetary Rings", variable=self.ring_enabled_var,
                        command=self.toggle_ring_panel).grid(row=4, column=0, columnspan=3, sticky=tk.W, pady=2)

        # --- RING PANEL (hidden until "Add Planetary Rings" is ticked) ---
        self.ring_frame = ttk.LabelFrame(right_panel, text="Planetary Rings", padding="10")
        ttk.Button(self.ring_frame, text="Select Ring Band (1px colour strip)",
                   command=self.browse_ring_band).grid(row=0, column=0, columnspan=2, sticky="ew", pady=2)
        ttk.Label(self.ring_frame, textvariable=self.ring_path, wraplength=300).grid(row=1, column=0, columnspan=2, sticky=tk.W, pady=2)
        self.ring_strip_label = ttk.Label(self.ring_frame, text="(no band loaded)")
        self.ring_strip_label.grid(row=2, column=0, columnspan=2, pady=2)
        ttk.Label(self.ring_frame, text="inner edge on left  |  outer edge on right").grid(row=3, column=0, columnspan=2, pady=(0, 4))
        self.add_spinbox_row(self.ring_frame, "Distance from Planet (radii):", self.ring_inner_var, 1.05, 4.0, 4, increment=0.05, fmt="%.2f")
        self.add_spinbox_row(self.ring_frame, "Ring Width (radii):", self.ring_width_var, 0.05, 4.0, 5, increment=0.05, fmt="%.2f")
        self.add_spinbox_row(self.ring_frame, "Opacity (%):", self.ring_opacity_var, 0, 100, 6, increment=5)
        ttk.Checkbutton(self.ring_frame, text="Flip band direction", variable=self.ring_flip_var,
                        command=self.refresh_ring_strip).grid(row=7, column=0, columnspan=2, sticky=tk.W, pady=2)
        ttk.Label(self.ring_frame, textvariable=self.ring_info_var, wraplength=300).grid(row=8, column=0, columnspan=2, sticky=tk.W, pady=2)

        # --- BOTTOM SPAN (Progress & Action) ---
        bottom_frame = ttk.Frame(main_frame)
        bottom_frame.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(15, 0))

        self.progress = ttk.Progressbar(bottom_frame, orient=tk.HORIZONTAL, mode='determinate')
        self.progress.pack(fill=tk.X, pady=5)
        
        self.status_var = tk.StringVar(value="Waiting for input...")
        ttk.Label(bottom_frame, textvariable=self.status_var).pack()

        button_row = ttk.Frame(bottom_frame)
        button_row.pack(pady=10)
        self.preview_btn = ttk.Button(button_row, text="Preview Frame", command=self.start_preview)
        self.preview_btn.pack(side=tk.LEFT, padx=5)
        self.generate_btn = ttk.Button(button_row, text="Generate Rotating Globe", command=self.start_generation)
        self.generate_btn.pack(side=tk.LEFT, padx=5)

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

    def _set_busy(self, busy):
        state = tk.DISABLED if busy else tk.NORMAL
        self.generate_btn.config(state=state)
        self.preview_btn.config(state=state)

    def update_output_extension(self, event=None):
        current_path = self.output_path.get()
        if current_path:
            base, ext = os.path.splitext(current_path)
            new_ext = self.output_format_var.get().lower()
            self.output_path.set(f"{base}.{new_ext}")

    def add_spinbox_row(self, parent, label, var, vmin, vmax, row, increment=1, values=None, fmt=None):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky=tk.W, pady=4)
        if values:
            ttk.Combobox(parent, textvariable=var, values=values, state="readonly", width=8).grid(row=row, column=1, sticky=tk.E, padx=5)
        else:
            extra = {"format": fmt} if fmt else {}
            ttk.Spinbox(parent, from_=vmin, to=vmax, increment=increment, textvariable=var, width=9, **extra).grid(row=row, column=1, sticky=tk.E, padx=5)

    # ------------------------------------------------------------------
    #  Ring panel
    # ------------------------------------------------------------------
    def toggle_ring_panel(self):
        if self.ring_enabled_var.get():
            self.ring_frame.pack(fill=tk.X, pady=(10, 0), after=self.aes_frame)
        else:
            self.ring_frame.pack_forget()
        self.update_ring_info()
        self.root.geometry("")          # let the window grow/shrink to fit the panel
        self.root.update_idletasks()

    def update_ring_info(self, *_):
        try:
            r_out = max(1.05, self.ring_inner_var.get()) + max(0.02, self.ring_width_var.get())
        except tk.TclError:
            return                      # field is mid-edit / not a number yet
        pct = 100.0 / (r_out * RING_MARGIN)
        self.ring_info_var.set(f"Outer edge at {r_out:.2f} radii: the planet will fill {pct:.0f}% of the frame width.")

    def browse_ring_band(self):
        file_path = filedialog.askopenfilename(filetypes=[("Image Files", ("*.png", "*.jpg", "*.jpeg", "*.webp", "*.bmp", "*.gif"))])
        if not file_path:
            return
        try:
            band, (w, h) = load_ring_band(file_path)
        except Exception as e:
            messagebox.showerror("Error", f"Could not load ring band: {e}")
            return
        if min(w, h) > 4:
            messagebox.showinfo(
                "Ring Band",
                f"This image is {w}x{h}. A 1px strip is expected, so it will be averaged across its "
                f"short side and read {'left to right' if w >= h else 'top to bottom'} as inner to outer edge."
            )
        self.ring_band = band
        self.ring_path.set(file_path)
        self.refresh_ring_strip()

    def refresh_ring_strip(self):
        if self.ring_band is None:
            return
        band = prepare_band(self.ring_band, self.ring_flip_var.get(), 100)
        self.ring_strip_photo = ImageTk.PhotoImage(band_preview_image(band))
        self.ring_strip_label.config(image=self.ring_strip_photo, text="")

    # ------------------------------------------------------------------
    #  Input / output selection
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    #  Settings, preview, generation
    # ------------------------------------------------------------------
    def _collect_settings(self):
        """Read every Tk variable on the UI thread; workers only ever see plain values."""
        if not self.input_path.get():
            messagebox.showwarning("Missing Input", "Please select an input map image.")
            return None

        ring_on = self.ring_enabled_var.get()
        if ring_on and self.ring_band is None:
            messagebox.showwarning("Missing Ring Band", "Rings are enabled, but no ring band image has been selected.")
            return None

        try:
            cfg = {
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
                "ring_enabled": ring_on,
                "ring_band": self.ring_band,
                "ring_flip": self.ring_flip_var.get(),
                "ring_opacity": self.ring_opacity_var.get(),
            }
            r_in = max(1.05, self.ring_inner_var.get())
            r_out = r_in + max(0.02, self.ring_width_var.get())
        except tk.TclError:
            messagebox.showwarning("Invalid Setting", "One of the settings is not a valid number.")
            return None

        cfg["ring_inner"] = r_in
        cfg["ring_outer"] = r_out
        # Planet shrinks to leave room for the rings (full frame when rings are off)
        cfg["planet_frac"] = 1.0 / (r_out * RING_MARGIN) if ring_on else 1.0
        return cfg

    def start_preview(self):
        if self.is_rendering:
            return
        cfg = self._collect_settings()
        if cfg is None:
            return
        self.is_rendering = True
        self._set_busy(True)
        self.status_var.set("Rendering preview frame...")
        threading.Thread(target=self.preview_loop, args=(cfg,), daemon=True).start()

    def preview_loop(self, cfg):
        try:
            ctx = prepare_render(cfg, status=lambda msg: self._post(self.status_var.set, msg))
            frame = render_frame(cfg, ctx, 0)
            if cfg["trans_bg"]:
                bg = checker_background(frame.size[0])
            else:
                bg = Image.new("RGBA", frame.size, (15, 15, 15, 255))
            composite = Image.alpha_composite(bg, frame).convert("RGB")
            self._post(self._show_preview, composite)
            self._post(self.status_var.set, "Preview ready.")
        except Exception as e:
            self._post(self.status_var.set, "Error during preview.")
            self._post(messagebox.showerror, "Preview Error", str(e))
        finally:
            self.is_rendering = False
            self._post(self._set_busy, False)

    def _show_preview(self, pil_img):
        max_px = 640
        if pil_img.width > max_px:
            pil_img = pil_img.resize((max_px, max_px), Image.LANCZOS)   # display only
        self.preview_win_photo = ImageTk.PhotoImage(pil_img)
        if self.preview_win is None or not self.preview_win.winfo_exists():
            self.preview_win = tk.Toplevel(self.root)
            self.preview_win.title("Frame Preview (Lon 0°)")
            self.preview_win_label = ttk.Label(self.preview_win)
            self.preview_win_label.pack()
        self.preview_win_label.config(image=self.preview_win_photo)

    def start_generation(self):
        if self.is_rendering: return
        cfg = self._collect_settings()
        if cfg is None:
            return

        # Transparent frames are all held in memory as RGBA before encoding
        est_bytes = cfg["size"] ** 2 * 4 * (360 // cfg["step_deg"])
        if est_bytes > 1e9:
            if not messagebox.askokcancel(
                "High Memory Use",
                f"These settings may need roughly {est_bytes / 1e9:.1f} GB of RAM while rendering.\n\n"
                "Consider a smaller output size or a larger degrees/frame value.\n\nContinue anyway?"
            ):
                return

        self.is_rendering = True
        self._set_busy(True)
        # Reset progress bar to determinate for the rendering loop
        self.progress.config(mode='determinate', value=0)

        threading.Thread(target=self.render_loop, args=(cfg,), daemon=True).start()

    def render_loop(self, cfg):
        try:
            out_file = cfg["out_file"]
            step_deg = cfg["step_deg"]
            fps = cfg["fps"]
            trans_bg = cfg["trans_bg"]
            output_format = cfg["output_format"]

            frames = []

            total_frames = 360 // step_deg
            self._post(self.progress.config, {'maximum': total_frames})

            # Per-render setup: map, lighting mask, and (if enabled) the static ring layers
            ctx = prepare_render(cfg, status=lambda msg: self._post(self.status_var.set, msg))

            for i, lon in enumerate(range(0, 360, step_deg)):
                self._post(self.status_var.set, f"Rendering Frame {i+1}/{total_frames} (Lon {lon}°)")

                frame = render_frame(cfg, ctx, lon)

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
            self._post(self._set_busy, False)

if __name__ == "__main__":
    root = tk.Tk()
    app = GlobeGifApp(root)
    root.mainloop()
