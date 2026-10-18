import { staticFile } from "remotion";

// Same palette as the Nama Kadhai app and caption style.
export const colors = {
  paper: "#F4ECDD",
  ink: "#281D18",
  cream: "#FFF4DE",
  marigold: "#D6A638",
  marigoldDark: "#805414",
  night: "#141010",
};

// Poppins carries the Latin/Tanglish text; Nirmala UI (Windows) covers Tamil script.
export const fontFamily = "'NamaKadhaiPoppins', 'Nirmala UI', 'Noto Sans Tamil', sans-serif";

let fontPromise: Promise<void> | null = null;

export const loadFonts = (): Promise<void> => {
  if (!fontPromise) {
    const face = new FontFace("NamaKadhaiPoppins", `url(${staticFile("Poppins-Bold.ttf")})`, {
      weight: "700",
    });
    fontPromise = face.load().then((loaded) => {
      document.fonts.add(loaded);
    });
  }
  return fontPromise;
};

export type CardFormat = { width: number; height: number; fps: number };

export type CardProps = CardFormat & {
  title: string;
  subtitle: string;
  durationSeconds: number;
};

/** Defensive limits; the Python side validates the same rules before rendering. */
export const clampProps = (props: CardProps): CardProps => ({
  ...props,
  title: String(props.title ?? "").slice(0, 80),
  subtitle: String(props.subtitle ?? "").slice(0, 120),
  durationSeconds: Math.min(10, Math.max(1, Number(props.durationSeconds) || 3)),
  width: Math.min(3840, Math.max(320, Math.round(Number(props.width) || 1080))),
  height: Math.min(3840, Math.max(320, Math.round(Number(props.height) || 1920))),
  fps: Math.min(60, Math.max(24, Math.round(Number(props.fps) || 30))),
});
