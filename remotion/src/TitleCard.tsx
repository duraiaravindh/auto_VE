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

const useFonts = () => {
  const [handle] = useState(() => delayRender("Loading Poppins"));
  useEffect(() => {
    loadFonts().then(() => continueRender(handle), () => continueRender(handle));
  }, [handle]);
};

/** Opening card: title springs in, a marigold line draws under it, subtitle rises. */
export const TitleCard: React.FC<CardProps> = (raw) => {
  useFonts();
  const props = clampProps(raw);
  const frame = useCurrentFrame();
  const { fps, durationInFrames, width, height } = useVideoConfig();
  const short = Math.min(width, height);

  const pop = spring({ frame, fps, config: { damping: 14, stiffness: 160 } });
  const line = interpolate(frame, [fps * 0.25, fps * 0.85], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const sub = spring({ frame: frame - fps * 0.45, fps, config: { damping: 18 } });
  const exit = interpolate(frame, [durationInFrames - fps * 0.4, durationInFrames], [1, 0], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  return (
    <AbsoluteFill
      style={{
        background: `radial-gradient(circle at 50% 40%, ${colors.ink} 0%, ${colors.night} 75%)`,
        alignItems: "center",
        justifyContent: "center",
        fontFamily,
        opacity: exit,
      }}
    >
      <div style={{ textAlign: "center", padding: short * 0.08 }}>
        <div
          style={{
            color: colors.cream,
            fontWeight: 700,
            fontSize: short * 0.1,
            lineHeight: 1.15,
            transform: `scale(${0.85 + 0.15 * pop})`,
            opacity: pop,
          }}
        >
          {props.title}
        </div>
        <div
          style={{
            height: short * 0.008,
            width: `${line * 60}%`,
            margin: `${short * 0.03}px auto`,
            background: colors.marigold,
            borderRadius: short * 0.004,
          }}
        />
        {props.subtitle ? (
          <div
            style={{
              color: colors.marigold,
              fontWeight: 700,
              fontSize: short * 0.045,
              transform: `translateY(${(1 - sub) * short * 0.03}px)`,
              opacity: Math.max(0, sub),
            }}
          >
            {props.subtitle}
          </div>
        ) : null}
      </div>
    </AbsoluteFill>
  );
};
