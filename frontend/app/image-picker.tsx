"use client";

// The runtime-image picker shown on the new-thread screen. Images come from
// the bridge's GET /agui/images (one entry per configured backend, first =
// default — e.g. "default" and "languages", the WASM-language toolbox image).
// The choice only matters for the thread's FIRST run (creation); existing
// threads stay on the backend they were created on, so the picker is only
// rendered while the thread is still empty.

export type RuntimeImage = {
  name: string;
  default: boolean;
  languages: boolean;
};

export async function fetchRuntimeImages(bridge: string): Promise<RuntimeImage[]> {
  try {
    const r = await fetch(`${bridge}/agui/images`);
    const d = await r.json();
    return Array.isArray(d.images) ? d.images : [];
  } catch {
    return []; // bridge down or pre-images bridge — picker just stays hidden
  }
}

export function ImagePicker({
  images,
  value,
  onChange,
}: {
  images: RuntimeImage[];
  value: string;
  onChange: (name: string) => void;
}) {
  // Nothing to pick with zero/one image — keep the screen unchanged.
  if (images.length < 2) return null;
  return (
    <label className="image-picker" data-testid="image-picker">
      <span className="image-picker-label">runtime image</span>
      <select
        className="image-picker-select"
        data-testid="image-select"
        value={value}
        onChange={(e) => onChange(e.target.value)}
      >
        {images.map((img) => (
          <option key={img.name} value={img.name}>
            {img.name}
            {img.default ? " (default)" : ""}
          </option>
        ))}
      </select>
    </label>
  );
}
