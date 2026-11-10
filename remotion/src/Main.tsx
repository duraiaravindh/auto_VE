import React, {useEffect, useState} from 'react';
import {
	AbsoluteFill,
	OffthreadVideo,
	cancelRender,
	continueRender,
	delayRender,
	interpolate,
	spring,
	staticFile,
	useCurrentFrame,
} from 'remotion';
import data from './data.json';
import {SceneArt} from './Scenes';

export const NAVY = '#06101F';
export const MINT = '#2EF2C9';
export const FPS = 30;
const SLIDE = 8; // frames for every layout move

const fonts: [string, string][] = [
	['Poppins', 'fonts/Poppins-Bold.ttf'],
	['Anton', 'fonts/Anton-Regular.ttf'],
	['Kaushan', 'fonts/KaushanScript-Regular.ttf'],
	['Crimson', 'fonts/CrimsonText-Regular.ttf'],
	['Crimson', 'fonts/CrimsonText-Regular.ttf'],
];

const useFonts = () => {
	const [handle] = useState(() => delayRender('fonts'));
	useEffect(() => {
		Promise.all(
			fonts.map(([family, file]) => {
				const face = new FontFace(family, `url(${staticFile(file)})`);
				return face.load().then((f) => document.fonts.add(f));
			}),
		)
			.then(() => continueRender(handle))
			.catch((e) => cancelRender(e));
	}, [handle]);
};

type Scene = (typeof data.scenes)[number];
const activeScene = (t: number): Scene | undefined => data.scenes.find((s) => t >= s.start && t < s.end);

// 0 to 1 on the way in, 1 to 0 on the way out, in frames around a scene's window
const presence = (frame: number, s: Scene) =>
	Math.min(
		interpolate(frame, [s.start * FPS, s.start * FPS + SLIDE], [0, 1], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'}),
		interpolate(frame, [s.end * FPS - SLIDE, s.end * FPS], [1, 0], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'}),
	);

const ease = (x: number) => 1 - Math.pow(1 - x, 3);

const Backdrop: React.FC = () => (
	<AbsoluteFill style={{background: NAVY}}>
		<AbsoluteFill style={{background: 'radial-gradient(circle at 50% 30%, rgba(30,120,220,.45), rgba(6,16,31,0) 62%)'}} />
		<AbsoluteFill
			style={{
				backgroundImage:
					'linear-gradient(rgba(120,190,255,.08) 2px, transparent 2px), linear-gradient(90deg, rgba(120,190,255,.08) 2px, transparent 2px)',
				backgroundSize: '90px 90px',
			}}
		/>
	</AbsoluteFill>
);

// reel5 style: plain serif in soft gold with a soft shadow, cut in with no pop or highlight
const SUB_SIZE = 56;
const SUB_BOTTOM = 1560;
const subLines = (text: string): string[] => {
	if (text.length * SUB_SIZE * 0.42 <= 940) return [text];
	const words = text.split(' ');
	let best = 1;
	let score = Infinity;
	for (let i = 1; i < words.length; i++) {
		const worst = Math.max(words.slice(0, i).join(' ').length, words.slice(i).join(' ').length);
		if (worst < score) {
			score = worst;
			best = i;
		}
	}
	return [words.slice(0, best).join(' '), words.slice(best).join(' ')];
};

const Caption: React.FC<{frame: number}> = ({frame}) => {
	const t = frame / FPS;
	const p = data.phrases.find((x) => t >= x.start && t < x.end);
	if (!p) return null;
	return (
		<div style={{position: 'absolute', left: 40, width: 1000, bottom: 1920 - SUB_BOTTOM, textAlign: 'center', fontFamily: 'Crimson', fontSize: SUB_SIZE, lineHeight: 1.12, color: '#F7D94C', textShadow: '0 0 2px #000, 0 1px 2px #000, 1px 2px 3px rgba(0,0,0,.95), 0 0 9px rgba(0,0,0,.75)'}}>
			{subLines(p.text).map((line, i) => (
				<div key={i} style={{whiteSpace: 'nowrap'}}>{line}</div>
			))}
		</div>
	);
};

const Hook: React.FC<{frame: number}> = ({frame}) => {
	if (frame >= 2.6 * FPS) return null;
	const y = interpolate(frame, [0, 6], [-40, 0], {extrapolateRight: 'clamp'});
	const out = interpolate(frame, [2.4 * FPS, 2.6 * FPS], [1, 0], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'});
	const [first, second] = ['Ungalukku ippadi oru', 'friend irukkaangala?'];
	return (
		<div style={{position: 'absolute', top: 224 + y, left: 70, width: 800, textAlign: 'center', opacity: out, fontFamily: 'Anton', lineHeight: 1.1, WebkitTextStroke: '10px #03060c', paintOrder: 'stroke fill'}}>
			<div style={{fontSize: 84, color: 'white', whiteSpace: 'nowrap'}}>{first}</div>
			<div style={{fontSize: 88, color: 'white', whiteSpace: 'nowrap'}}>
				<span style={{color: MINT}}>friend</span> irukkaangala?
			</div>
			<span style={{display: 'none'}}>{second}</span>
		</div>
	);
};

export const Main: React.FC = () => {
	useFonts();
	const frame = useCurrentFrame();
	const t = frame / FPS;
	const scene = activeScene(t);
	const endStart = data.total - 2.5;
	const ending = t >= endStart;

	const amount = scene ? ease(presence(frame, scene)) : ending ? ease(Math.min((t - endStart) * FPS / SLIDE, 1)) : 0;
	const split = (scene && scene.layout === 'split') || ending;
	const panel = split
		? {
				left: interpolate(amount, [0, 1], [0, 120]),
				top: interpolate(amount, [0, 1], [0, 960]),
				width: interpolate(amount, [0, 1], [1080, 840]),
				height: interpolate(amount, [0, 1], [1920, 470]),
				radius: interpolate(amount, [0, 1], [0, 42]),
		  }
		: {left: 0, top: 0, width: 1080, height: 1920, radius: 0};

	const fullCover = scene && scene.layout === 'full';
	const showBackdrop = amount > 0 && (split || fullCover);

	return (
		<AbsoluteFill style={{background: NAVY}}>
			{showBackdrop && split ? <Backdrop /> : null}
			<div style={{position: 'absolute', left: panel.left, top: panel.top, width: panel.width, height: panel.height, borderRadius: panel.radius, overflow: 'hidden', boxShadow: split ? `0 0 ${60 * amount}px rgba(46,242,201,.35)` : undefined}}>
				<OffthreadVideo muted src={staticFile('base.mp4')} style={{width: '100%', height: '100%', objectFit: 'cover', objectPosition: '50% 31%'}} />
			</div>
			{scene ? (
				<AbsoluteFill style={fullCover ? {transform: `translateX(${(1 - amount) * 1080}px)`} : {opacity: amount, transform: `translateY(${(1 - amount) * -60}px)`}}>
					{fullCover ? <Backdrop /> : null}
					<SceneArt scene={scene} localFrame={frame - scene.start * FPS} duration={(scene.end - scene.start) * FPS} />
				</AbsoluteFill>
			) : null}
			{ending ? (
				<div style={{position: 'absolute', top: 240, left: 90, width: 760, opacity: amount, textAlign: 'center', transform: `translateY(${(1 - amount) * -60}px)`}}>
					<div style={{fontFamily: 'Poppins', fontWeight: 700, fontSize: 42, lineHeight: 1.25, color: 'white'}}>{data.endLine}</div>
					<div style={{marginTop: 26, fontFamily: 'Anton', fontSize: 84, whiteSpace: 'nowrap', color: NAVY, background: MINT, borderRadius: 24, padding: '8px 10px'}}>{data.endCall}</div>
				</div>
			) : null}
			<Caption frame={frame} />
			<Hook frame={frame} />
		</AbsoluteFill>
	);
};
