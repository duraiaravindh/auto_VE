import React from "react";
import { CalculateMetadataFunction, Composition } from "remotion";
import { Outro } from "./Outro";
import { CardProps, clampProps } from "./theme";
import { TitleCard } from "./TitleCard";

// Size, frame rate and length come from the props, so one composition renders
// both the landscape (1920x1080) and vertical (1080x1920) versions.
const fromProps: CalculateMetadataFunction<CardProps> = ({ props }) => {
  const safe = clampProps(props);
  return {
    width: safe.width,
    height: safe.height,
    fps: safe.fps,
    durationInFrames: Math.round(safe.durationSeconds * safe.fps),
    props: safe,
  };
};

const defaults: CardProps = {
  title: "Nama Kadhai",
  subtitle: "Oru chinna kadhai",
  durationSeconds: 3,
  width: 1080,
  height: 1920,
  fps: 30,
};

export const Root: React.FC = () => (
  <>
    <Composition
      id="TitleCard"
      component={TitleCard}
      defaultProps={defaults}
      calculateMetadata={fromProps}
      width={1080}
      height={1920}
      fps={30}
      durationInFrames={90}
    />
    <Composition
      id="Outro"
      component={Outro}
      defaultProps={{ ...defaults, subtitle: "Comment-la sollunga" }}
      calculateMetadata={fromProps}
      width={1080}
      height={1920}
      fps={30}
      durationInFrames={90}
    />
  </>
);
