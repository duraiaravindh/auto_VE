import React, {useEffect, useState} from 'react';
import {
	AbsoluteFill,
	OffthreadVideo,
	cancelRender,
	continueRender,
	delayRender,
	interpolate,
	staticFile,
	useCurrentFrame,
} from 'remotion';
import data from './data5.json';

// Typography system taken from reference reel5; colours sampled by eye from its frames
const YELLOW = '#F5C533';
const CREAM = '#FDFBE7';
const FPS = 30;
const clamp = {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'} as const;

const fonts: [string, string][] = [
	['Anton', 'fonts/Anton-Regular.ttf'],
	['Script', 'fonts/Sacramento-Regular.ttf'],
	['Crimson', 'fonts/CrimsonText-Regular.ttf'],
	['Poppins', 'fonts/Poppins-Bold.ttf'],
];

const useFonts = () => {
	const [handle] = useState(() => delayRender('fonts'));
	useEffect(() => {
		Promise.all(fonts.map(([family, file]) => new FontFace(family, `url(${staticFile(file)})`).load().then((f) => document.fonts.add(f))))
			.then(() => continueRender(handle))
			.catch((e) => cancelRender(e));
	}, [handle]);
};

const fit = (text: string, max: number, width = 900) => Math.min(max, width / (text.length * 0.43));
type Moment = (typeof data.moments)[number];

// Big capitals above the head: quick settle in, blur away on the way out
const Headline: React.FC<{m: Moment; f: number; length: number}> = ({m, f, length}) => {
	const size = fit(m.text, 190);
	const leave = interpolate(f, [length - 7, length], [0, 1], clamp);
	return (
		<div style={{position: 'absolute', top: 232, left: 0, width: 1080, textAlign: 'center', opacity: interpolate(f, [0, 2], [0, 1], clamp) * (1 - leave), filter: `blur(${leave * 14}px)`, transform: `scale(${interpolate(f, [0, 6], [0.96, 1], clamp)})`}}>
			{m.kicker ? (
				<div style={{fontFamily: 'Script', fontSize: 70, lineHeight: 0.9, color: 'white', textShadow: '0 1px 6px rgba(0,0,0,.8)', marginBottom: -4}}>{m.kicker}</div>
			) : null}
			<div style={{fontFamily: 'Anton', fontSize: size, lineHeight: 1.08, color: YELLOW, whiteSpace: 'nowrap', textShadow: '0 3px 14px rgba(0,0,0,.55)'}}>{m.text}</div>
		</div>
	);
};

// Cream card: tiny script over a yellow heading
const CreamCard: React.FC<{m: Moment; f: number}> = ({m, f}) => (
	<AbsoluteFill style={{background: CREAM}}>
		<div style={{position: 'absolute', top: 690, left: 0, width: 1080, textAlign: 'center'}}>
			{m.kicker ? <div style={{fontFamily: 'Script', fontSize: 78, lineHeight: 0.95, color: '#2a2a2a', opacity: interpolate(f, [0, 6], [0, 1], clamp)}}>{m.kicker}</div> : null}
			<div style={{fontFamily: 'Anton', fontSize: fit(m.text, 200), lineHeight: 1.1, color: YELLOW, whiteSpace: 'nowrap', opacity: interpolate(f, [4, 10], [0, 1], clamp), transform: `translateY(${interpolate(f, [4, 12], [18, 0], clamp)}px)`}}>{m.text}</div>
		</div>
	</AbsoluteFill>
);

// Grid paper: a drawn line leads into the yellow words
const GridCard: React.FC<{m: Moment; f: number}> = ({m, f}) => {
	const size = fit(m.text, 150, 700);
	const draw = interpolate(f, [0, 12], [0, 1], clamp);
	const width = m.text.length * size * 0.43;
	const left = (1080 - width) / 2;
	return (
		<AbsoluteFill style={{background: '#CFD2CC'}}>
			<AbsoluteFill style={{backgroundImage: 'linear-gradient(rgba(70,110,110,.35) 2px, transparent 2px), linear-gradient(90deg, rgba(70,110,110,.35) 2px, transparent 2px)', backgroundSize: '72px 72px'}} />
			<AbsoluteFill style={{background: 'radial-gradient(circle at 50% 45%, rgba(0,0,0,0) 45%, rgba(0,0,0,.42) 100%)'}} />
			<div style={{position: 'absolute', top: 842, left: 0, height: 6, width: Math.max(left - 24, 0) * draw, background: YELLOW}} />
			<div style={{position: 'absolute', top: 760, left: 0, width: 1080, textAlign: 'center', fontFamily: 'Anton', fontSize: size, lineHeight: 1.1, color: YELLOW, whiteSpace: 'nowrap', opacity: interpolate(f, [8, 13], [0, 1], clamp), textShadow: '0 2px 8px rgba(0,0,0,.25)'}}>{m.text}</div>
		</AbsoluteFill>
	);
};

// Giant end card, as at the end of reel5
const EndCard: React.FC<{f: number}> = ({f}) => (
	<AbsoluteFill style={{background: CREAM, opacity: interpolate(f, [0, 3], [0, 1], clamp)}}>
		<div style={{position: 'absolute', top: 300, left: 0, width: 1080, textAlign: 'center'}}>
			<div style={{fontFamily: 'Poppins', fontSize: 34, lineHeight: 1.3, color: '#2a2a2a', padding: '0 120px'}}>{data.endLine}</div>
			<div style={{marginTop: 46, fontFamily: 'Script', fontSize: 150, lineHeight: 0.9, color: '#2a2a2a'}}>Comment-la</div>
			<div style={{marginTop: 20, fontFamily: 'Anton', fontSize: 300, lineHeight: 1.05, color: YELLOW, transform: `scale(${interpolate(f, [0, 10], [0.94, 1], clamp)})`}}>SOLLUNGA</div>
		</div>
	</AbsoluteFill>
);

const Hook: React.FC<{f: number}> = ({f}) => {
	if (f >= 2.6 * FPS) return null;
	const leave = interpolate(f, [2.6 * FPS - 7, 2.6 * FPS], [0, 1], clamp);
	return (
		<div style={{position: 'absolute', top: 228, left: 0, width: 1080, textAlign: 'center', opacity: 1 - leave, filter: `blur(${leave * 14}px)`}}>
			<div style={{fontFamily: 'Script', fontSize: 82, lineHeight: 0.92, color: 'white', textShadow: '0 1px 6px rgba(0,0,0,.8)'}}>Ungalukku ippadi oru</div>
			<div style={{fontFamily: 'Anton', fontSize: 104, lineHeight: 1.12, color: YELLOW, whiteSpace: 'nowrap', textShadow: '0 3px 14px rgba(0,0,0,.55)'}}>FRIEND IRUKKAANGALA?</div>
		</div>
	);
};

const SUB_SIZE = 56;
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

const Subtitle: React.FC<{t: number}> = ({t}) => {
	const p = data.phrases.find((x) => t >= x.start && t < x.end);
	if (!p) return null;
	return (
		<div style={{position: 'absolute', left: 40, width: 1000, bottom: 360, textAlign: 'center', fontFamily: 'Crimson', fontSize: SUB_SIZE, lineHeight: 1.12, color: '#F7D94C', textShadow: '0 0 2px #000, 0 1px 2px #000, 1px 2px 3px rgba(0,0,0,.95), 0 0 9px rgba(0,0,0,.75)'}}>
			{subLines(p.text).map((line, i) => (
				<div key={i} style={{whiteSpace: 'nowrap'}}>{line}</div>
			))}
		</div>
	);
};

export const Reel5: React.FC = () => {
	useFonts();
	const frame = useCurrentFrame();
	const t = frame / FPS;
	const m = data.moments.find((x) => t >= x.start && t < x.end);
	const f = m ? frame - m.start * FPS : 0;
	const endStart = data.total - data.endSeconds;
	return (
		<AbsoluteFill style={{background: 'black'}}>
			<OffthreadVideo muted src={staticFile('base.mp4')} style={{width: '100%', height: '100%', objectFit: 'cover'}} />
			{m && m.type === 'cream' ? <CreamCard m={m} f={f} /> : null}
			{m && m.type === 'grid' ? <GridCard m={m} f={f} /> : null}
			{m && m.type === 'head' ? <Headline m={m} f={f} length={(m.end - m.start) * FPS} /> : null}
			<Hook f={frame} />
			{t >= endStart ? <EndCard f={frame - endStart * FPS} /> : null}
			<Subtitle t={t} />
		</AbsoluteFill>
	);
};
