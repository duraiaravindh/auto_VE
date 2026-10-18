import React, { useEffect, useState } from "react";
import {
  AbsoluteFill,
  continueRender,
  delayRender,
  interpolate,
  spring,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import { CardProps, clampProps, colors, fontFamily, loadFonts } from "./theme";

/** Closing card: channel name drops in, then a call-to-action line pulses once. */
export const Outro: React.FC<CardProps> = (raw) => {
  const [handle] = useState(() => delayRender("Loading Poppins"));
  useEffect(() => {
    loadFonts().then(() => continueRender(handle), () => continueRender(handle));
  }, [handle]);
  const props = clampProps(raw);
  const frame = useCurrentFrame();
  const { fps, durationInFrames, width, height } = useVideoConfig();
  const short = Math.min(width, height);

  const drop = spring({ frame, fps, config: { damping: 12 } });
  const cta = spring({ frame: frame - fps * 0.6, fps, config: { damping: 10, stiffness: 120 } });
  const enter = interpolate(frame, [0, fps * 0.3], [0, 1], { extrapolateRight: "clamp" });

  return (
    <AbsoluteFill
      style={{
        background: colors.paper,
        alignItems: "center",
        justifyContent: "center",
        fontFamily,
        opacity: enter * interpolate(frame, [durationInFrames - fps * 0.3, durationInFrames], [1, 0], {
          extrapolateLeft: "clamp",
          extrapolateRight: "clamp",
        }),
      }}
    >
      <div style={{ textAlign: "center", padding: short * 0.08 }}>
        <div
          style={{
            color: colors.ink,
            fontWeight: 700,
            fontSize: short * 0.11,
            transform: `translateY(${(1 - drop) * -short * 0.08}px)`,
          }}
        >
          {props.title}
        </div>
        {props.subtitle ? (
          <div
            style={{
              display: "inline-block",
              marginTop: short * 0.04,
              padding: `${short * 0.018}px ${short * 0.05}px`,
              borderRadius: short * 0.05,
              background: colors.marigoldDark,
              color: colors.cream,
              fontWeight: 700,
              fontSize: short * 0.042,
              transform: `scale(${Math.max(0, cta)})`,
            }}
          >
            {props.subtitle}
          </div>
        ) : null}
      </div>
    </AbsoluteFill>
  );
};
