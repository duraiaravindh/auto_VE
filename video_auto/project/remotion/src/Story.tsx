import React, {createContext, useContext, useEffect, useState} from 'react';
import {
	AbsoluteFill,
	Img,
	cancelRender,
	continueRender,
	delayRender,
	interpolate,
	random,
	spring,
	staticFile,
	useCurrentFrame,
} from 'remotion';

// Shared by every story: each Reel supplies its beats as data.
// The reel5 system: yellow condensed headlines, script kickers, grid paper with white-framed
// pictures, a tilted collage wall, cream cards and small gold serif subtitles.
const YELLOW = '#F5C533';
const CREAM = '#FDFBE7';
const INK = '#2a2112';
const FPS = 30;
const clamp = {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'} as const;

const fonts: [string, string, FontFaceDescriptors?][] = [
	['Anton', 'fonts/Anton-Regular.ttf'],
	['Script', 'fonts/Sacramento-Regular.ttf'],
	['Crimson', 'fonts/CrimsonText-Regular.ttf'],
	['Cinzel', 'fonts/Cinzel-Variable.ttf', {weight: '400 900'}],
	['AlfaSlab', 'fonts/AlfaSlabOne-Regular.ttf'],
	['Cormorant', 'fonts/CormorantGaramond-Variable.ttf', {weight: '300 700'}],
	['Playfair', 'fonts/PlayfairDisplay-Italic-Variable.ttf', {weight: '400 900', style: 'italic'}],
	['Kaushan', 'fonts/KaushanScript-Regular.ttf'],
	['Montserrat', 'fonts/Montserrat-ExtraBold.ttf', {weight: '800'}],
	['Baloo', 'fonts/Baloo2-Variable.ttf', {weight: '400 800'}],
	['SpecialElite', 'fonts/SpecialElite-Regular.ttf'],
];

// Type follows the story's mood. A beat with no theme keeps the brand look: yellow Anton.
const THEMES: Record<string, any> = {
	divine: {font: 'Cinzel', weight: 800, fill: 'linear-gradient(180deg, #FFFBE3 0%, #FFE27E 50%, #F0A92A 100%)', filter: 'drop-shadow(0 0 2px #2A1000) drop-shadow(0 3px 3px rgba(40,15,0,1)) drop-shadow(0 0 16px rgba(30,10,0,.85))', spacing: 0.02, motion: 'rise', kickerFont: 'Crimson', kickerColor: '#FFF4D0', kickerSize: 74, sub: '#FFE39A'},
	memory: {font: 'AlfaSlab', weight: 400, color: '#FFF0CF', shadow: '4px 4px 0 #B8501A, 8px 8px 0 rgba(50,20,0,.8), 0 0 22px rgba(30,10,0,.8)', spacing: 0.01, motion: 'pop', kickerFont: 'Kaushan', kickerColor: '#FFD98A', kickerSize: 70, sub: '#FFE9BD'},
	hush: {font: 'Cormorant', weight: 500, color: '#F6EFE0', shadow: '0 2px 16px rgba(0,0,0,.9)', spacing: 0.16, motion: 'fade', kickerFont: 'Cormorant', kickerColor: '#F6EFE0', kickerSize: 66, sub: '#F3EDE0'},
	now: {font: 'Montserrat', weight: 800, color: '#F2FBFF', shadow: '0 0 3px #04162B, 0 0 24px rgba(70,180,255,.85), 0 4px 14px rgba(0,10,30,.85)', spacing: -0.01, motion: 'pop', kickerFont: 'Montserrat', kickerColor: '#BEE9FF', kickerSize: 50, sub: '#E8F7FF'},
	wow: {font: 'Anton', weight: 400, fill: 'linear-gradient(180deg, #FFF16E 0%, #FF8A1F 100%)', filter: 'drop-shadow(0 4px 2px rgba(80,20,0,.9)) drop-shadow(0 0 22px rgba(255,120,30,.7))', spacing: 0, motion: 'pop', sub: '#FFE27A'},
	quiet: {font: 'SpecialElite', weight: 400, color: '#F2EFE6', shadow: '0 2px 10px rgba(0,0,0,.95), 0 0 3px #000', spacing: 0.02, motion: 'type', kickerFont: 'SpecialElite', kickerColor: '#F2EFE6', kickerSize: 54, sub: '#EDEBE4'},
	film: {font: 'SpecialElite', weight: 400, color: '#F7E6BE', shadow: '2px 2px 0 #4A2C10, 0 0 16px rgba(30,15,0,.9)', spacing: 0.02, motion: 'type', kickerFont: 'SpecialElite', kickerColor: '#F7E6BE', kickerSize: 54, sub: '#F7E6BE'},
	kids: {font: 'Baloo', weight: 800, palette: ['#FFD93B', '#FF7FB0', '#62DBFF', '#93F16F', '#FFAE5C'], shadow: '0 4px 0 #3B1466, 0 7px 14px rgba(0,0,0,.65)', spacing: 0, motion: 'bounce', sub: '#FFF3C9'},
	blast: {font: 'Anton', weight: 400, fill: 'linear-gradient(180deg, #FFF9B0 0%, #FFC21F 45%, #FF4D12 100%)', filter: 'drop-shadow(0 4px 2px rgba(60,10,0,.95)) drop-shadow(0 0 26px rgba(255,90,20,.9))', spacing: 0, motion: 'slam', sub: '#FFE27A'},
	sweet: {font: 'Kaushan', weight: 400, color: '#FFE2A8', shadow: '3px 3px 0 #A42C1C, 0 0 18px rgba(60,10,0,.75)', spacing: 0, motion: 'pop', kickerFont: 'Kaushan', kickerColor: '#FFF1D6', kickerSize: 66, sub: '#FFE9C4'},
	sky: {font: 'Cinzel', weight: 600, color: '#EAF4FF', shadow: '0 0 3px #00051A, 0 0 4px #00051A, 0 3px 12px rgba(0,5,30,1), 0 0 22px rgba(140,190,255,.8)', spacing: 0.06, motion: 'twinkle', sub: '#DDEBFF'},
	colour: {font: 'Baloo', weight: 800, palette: ['#FF5A8A', '#FFD93B', '#4DE3FF', '#9CFF6B', '#C98BFF', '#FFA64D'], shadow: '0 0 18px rgba(255,255,255,.55), 0 3px 8px rgba(0,0,0,.85)', spacing: 0, motion: 'burst', sub: '#FFF5D6'},
	tender: {font: 'Playfair', weight: 700, style: 'italic', color: '#FFF1CF', shadow: '0 2px 14px rgba(70,25,0,.95), 0 0 30px rgba(255,180,90,.6)', spacing: 0, motion: 'bloom', kickerFont: 'Crimson', kickerColor: '#FFF1CF', kickerSize: 62, sub: '#FFE9BD'},
	heart: {font: 'Playfair', weight: 800, style: 'italic', color: '#8E1F1B', spacing: 0, motion: 'pop', kickerFont: 'Crimson', kickerColor: INK, kickerSize: 62},
};

// Real width from the loaded font, so every typeface fills the line without leaving the frame
const measure = (text: string, th: any) => {
	const ctx = document.createElement('canvas').getContext('2d') as CanvasRenderingContext2D;
	ctx.font = `${th.style ?? 'normal'} ${th.weight} 100px ${th.font}`;
	const gaps = text.split(' ').length;
	return ctx.measureText(text).width / 100 + th.spacing * text.length + gaps * 0.22;
};
const fitTheme = (text: string, th: any, max: number, width = 910) => Math.min(max, width / measure(text, th));

const useFonts = () => {
	const [handle] = useState(() => delayRender('fonts'));
	const [ready, setReady] = useState(false);
	useEffect(() => {
		Promise.all(fonts.map(([family, file, options]) => new FontFace(family, `url(${staticFile(file)})`, options).load().then((f) => document.fonts.add(f))))
			.then(() => setReady(true))
			.catch((e) => cancelRender(e));
	}, []);
	useEffect(() => {
		if (ready) continueRender(handle);
	}, [ready, handle]);
	return ready;
};

type Beat = any;
const Story$ = createContext<any>(null);
const pad = (n: number) => String(n).padStart(5, '0');

// Measured on the Anton file: average capital about 0.46 em wide, including spacing
const fitAnton = (text: string, max: number, width = 910) => Math.min(max, width / (text.length * 0.46));

// Old film: dark corners, specks and the odd scratch, different on every frame
const Film: React.FC<{f: number; seed: string}> = ({f, seed}) => (
	<AbsoluteFill style={{pointerEvents: 'none'}}>
		<AbsoluteFill style={{background: 'radial-gradient(circle at 50% 46%, rgba(0,0,0,0) 45%, rgba(40,18,0,.62) 100%)'}} />
		{Array.from({length: 26}, (_, k) => {
			const r = (s: string) => random(`${s}${seed}${f}${k}`);
			return <div key={k} style={{position: 'absolute', left: `${r('x') * 100}%`, top: `${r('y') * 100}%`, width: 2 + r('w') * 4, height: 2 + r('h') * 5, borderRadius: '50%', background: r('c') > 0.5 ? 'rgba(25,12,0,.55)' : 'rgba(255,240,205,.4)'}} />;
		})}
		{random(`sc${seed}${Math.floor(f / 3)}`) > 0.55 ? <div style={{position: 'absolute', left: `${random(`sx${seed}${Math.floor(f / 3)}`) * 100}%`, top: 0, width: 2, height: '100%', background: 'rgba(255,240,205,.28)'}} /> : null}
	</AbsoluteFill>
);

const moodFilter = (b: Beat, f: number, frames: number) => {
	if (b.mood === 'fade') return `saturate(${interpolate(f, [0, frames], [1, 0.25], clamp)}) brightness(${interpolate(f, [0, frames], [1, 0.72], clamp)})`;
	if (b.mood === 'past') return 'sepia(.22) saturate(1.02) contrast(1.02)';
	if (b.mood === 'now') return 'contrast(1.08) saturate(1.12)';
	if (b.mood === 'film') return `sepia(.62) contrast(1.06) saturate(.92) brightness(${(0.965 + random(`fl${b.start}${f}`) * 0.07).toFixed(3)})`;
	return undefined;
};

const Clip: React.FC<{b: Beat; f: number}> = ({b, f}) => {
	const frames = (b.end - b.start) * FPS;
	const through = b.motion === 'through';
	const push = through
		? interpolate(f, [0, frames], [1, 3.6], {...clamp, easing: (x) => x * x * x})
		: b.motion === 'out' ? interpolate(f, [0, frames], [1.1, 1.02], clamp) : interpolate(f, [0, frames], [1.02, 1.1], clamp);
	return (
		<>
			<Img
				src={staticFile(`seq/${b.seq}/${pad(Math.min(f, (b.frames ?? 1) - 1))}.jpg`)}
				style={{position: 'absolute', inset: 0, width: '100%', height: '100%', objectFit: 'cover', objectPosition: `50% ${b.focusY ?? 50}%`, transform: `scale(${push * (b.zoom ?? 1)})`, transformOrigin: through ? '48% 50%' : b.zoom ? '50% 30%' : undefined, filter: [moodFilter(b, f, frames), b.amber ? 'sepia(.95) saturate(1.7) hue-rotate(-8deg) contrast(1.05) brightness(1.05)' : ''].join(' ').trim() || undefined}}
			/>
			{/* the past looks like an old TV: faint scanlines and darker corners */}
			{b.mood === 'past' ? <AbsoluteFill style={{background: 'radial-gradient(circle at 50% 48%, rgba(0,0,0,0) 52%, rgba(24,12,0,.42) 100%), repeating-linear-gradient(rgba(0,0,0,.075) 0 3px, rgba(0,0,0,0) 3px 7px)'}} /> : null}
			{b.mood === 'film' ? <Film f={f} seed={String(b.start)} /> : null}
			{through ? <AbsoluteFill style={{background: '#FFF4D6', opacity: interpolate(f, [frames * 0.68, frames], [0, 1], clamp)}} /> : null}
		</>
	);
};

// Night sky: stars that twinkle, or drift towards you when they feel close
const Stars: React.FC<{f: number; near?: boolean}> = ({f, near}) => (
	<AbsoluteFill style={{pointerEvents: 'none'}}>
		{Array.from({length: near ? 80 : 64}, (_, k) => {
			const x0 = random(`stx${k}`) * 1080;
			const y0 = random(`sty${k}`) * (near ? 1100 : 450);
			const spread = near ? 1 + f * 0.016 : 1;
			const size = (2 + random(`sts${k}`) * 5) * (near ? 1 + f * 0.03 : 1);
			return <div key={k} style={{position: 'absolute', left: 540 + (x0 - 540) * spread, top: 620 + (y0 - 620) * spread, width: size, height: size, borderRadius: '50%', background: '#F4FAFF', opacity: 0.35 + 0.6 * (0.5 + 0.5 * Math.sin(f / (3 + random(`stp${k}`) * 5) + k)), boxShadow: `0 0 ${size * 3}px #BFDcFF`}} />;
		})}
	</AbsoluteFill>
);

// Marigold petals falling through warm scenes
const Petals: React.FC<{f: number}> = ({f}) => (
	<AbsoluteFill style={{pointerEvents: 'none'}}>
		{Array.from({length: 26}, (_, k) => {
			const speed = 5 + random(`pv${k}`) * 6;
			const y = (random(`py${k}`) * 2100 + f * speed) % 2100 - 100;
			const size = 16 + random(`pz${k}`) * 20;
			return <div key={k} style={{position: 'absolute', left: random(`px${k}`) * 1080 + Math.sin(f / 11 + k) * 34, top: y, width: size, height: size * 0.62, borderRadius: '60% 40% 60% 40%', background: k % 3 ? '#FF9F1C' : '#FFD23F', opacity: 0.9, transform: `rotate(${f * (3 + k % 5) + k * 40}deg)`, boxShadow: '0 2px 5px rgba(90,30,0,.35)'}} />;
		})}
	</AbsoluteFill>
);

// Rockets climb on a glowing trail, then burst in colour
const ROCKET_COLOURS = ['#FF5A8A', '#FFD93B', '#4DE3FF', '#9CFF6B', '#C98BFF'];
const Rockets: React.FC<{f: number; seed: string}> = ({f, seed}) => (
	<AbsoluteFill style={{pointerEvents: 'none'}}>
		{[0, 1, 2, 3].flatMap((n) => {
			const start = n * 9;
			const x = 150 + random(`${seed}rx${n}`) * 780;
			const top = 330 + random(`${seed}ry${n}`) * 520;
			const tint = ROCKET_COLOURS[(n + seed.length) % ROCKET_COLOURS.length];
			const age = f - start;
			if (age < 0) return [];
			if (age < 11) {
				const y = interpolate(age, [0, 11], [1500, top], {easing: (v) => 1 - Math.pow(1 - v, 2)});
				return [<div key={`t${n}`} style={{position: 'absolute', left: x, top: y, width: 7, height: 130, borderRadius: 6, background: `linear-gradient(${tint}, rgba(255,255,255,0))`, boxShadow: `0 0 14px ${tint}`}} />];
			}
			const burst = age - 11;
			if (burst > 24) return [];
			return Array.from({length: 42}, (_, k) => {
				const a = (k / 42) * Math.PI * 2;
				const far = (170 + random(`${seed}rr${n}${k}`) * 170) * (1 - Math.pow(1 - Math.min(burst / 13, 1), 3));
				const size = Math.max(9 - burst * 0.22, 2);
				return <div key={`${n}-${k}`} style={{position: 'absolute', left: x + Math.cos(a) * far, top: top + Math.sin(a) * far + burst * burst * 0.11, width: size, height: size, borderRadius: '50%', background: tint, opacity: interpolate(burst, [13, 24], [1, 0], clamp), boxShadow: `0 0 ${size * 2.5}px ${tint}`}} />;
			});
		})}
	</AbsoluteFill>
);

// Slow gold dust rising through the frame, for the divine scenes
const Dust: React.FC<{f: number}> = ({f}) => (
	<AbsoluteFill style={{pointerEvents: 'none'}}>
		{Array.from({length: 54}, (_, k) => {
			const speed = 2 + random(`ds${k}`) * 4;
			const size = 4 + random(`dz${k}`) * 9;
			const y = (random(`dy${k}`) * 2100 - f * speed + 21000) % 2100 - 90;
			return <div key={k} style={{position: 'absolute', left: random(`dx${k}`) * 1080 + Math.sin(f / 13 + k) * 22, top: y, width: size, height: size, borderRadius: '50%', background: '#FFE7A0', opacity: 0.35 + 0.55 * random(`do${k}`), boxShadow: `0 0 ${size * 2}px #FFC94D`}} />;
		})}
	</AbsoluteFill>
);

// Gold firework bursts drawn in code, so they stay sharp and never repeat a stock asset
const Sparks: React.FC<{f: number; seed: string; y?: number}> = ({f, seed, y = 400}) => {
	const bursts = [0, 1, 2].map((i) => ({
		at: i * 9,
		x: 170 + random(`${seed}x${i}`) * 740,
		y: y - 130 + random(`${seed}y${i}`) * 300,
		tint: i === 1 ? '#FFF2B8' : YELLOW,
	}));
	return (
		<AbsoluteFill style={{pointerEvents: 'none'}}>
			{bursts.flatMap((b, i) => {
				const age = f - b.at;
				if (age < 0 || age > 26) return [];
				return Array.from({length: 34}, (_, k) => {
					const angle = (k / 34) * Math.PI * 2 + random(`${seed}a${i}${k}`) * 0.3;
					const reach = (130 + random(`${seed}r${i}${k}`) * 230) * (1 - Math.pow(1 - Math.min(age / 16, 1), 3));
					const fall = 0.32 * age * age * 0.28;
					const size = (13 - age * 0.32) * (0.6 + random(`${seed}s${i}${k}`) * 0.6);
					return (
						<div
							key={`${i}-${k}`}
							style={{
								position: 'absolute',
								left: b.x + Math.cos(angle) * reach,
								top: b.y + Math.sin(angle) * reach + fall,
								width: Math.max(size, 1),
								height: Math.max(size, 1),
								borderRadius: '50%',
								background: b.tint,
								opacity: interpolate(age, [16, 26], [1, 0], clamp),
								boxShadow: `0 0 ${Math.max(size * 2, 3)}px ${b.tint}`,
							}}
						/>
					);
				});
			})}
		</AbsoluteFill>
	);
};

// Words land one by one, then blur away, as the reel5 headlines do
const Headline: React.FC<{b: Beat; f: number; top?: number; dark?: boolean}> = ({b, f, top, dark}) => {
	if (!b.text && !b.kicker) return null;
	const length = (b.end - b.start) * FPS;
	const leave = b.hold ? 0 : interpolate(f, [length - 6, length], [0, 1], clamp);
	const lines = b.text ? b.text.split('|') : [];
	const th = THEMES[b.theme];
	const kickerFamily = b.kickerFont ?? th?.kickerFont ?? 'Script';
	let counter = 0;
	let letters = 0;
	// letter animation always finishes within the first half of the moment, however short it is
	const letterTotal = Math.max((b.text ?? '').replace(/[| ]/g, '').length, 1);
	const stagger = (base: number) => Math.min(base, (length * 0.5) / letterTotal);
	return (
		<div style={{position: 'absolute', top: top ?? 214, left: 0, width: 1080, textAlign: 'center', opacity: 1 - leave, filter: leave ? `blur(${leave * 16}px)` : undefined}}>
			{b.kicker ? (
				<div style={{fontFamily: kickerFamily, fontWeight: kickerFamily === 'Montserrat' ? 800 : undefined, fontSize: b.kickerSize ?? th?.kickerSize ?? 108, lineHeight: 1.05, color: dark ? INK : th?.kickerColor ?? 'white', opacity: interpolate(f, [0, th?.motion === 'fade' ? 14 : 4], [0, 1], clamp), WebkitTextStroke: kickerFamily === 'Script' ? `1.4px ${dark ? INK : 'white'}` : undefined, textShadow: b.soft ?? (dark ? 'none' : '0 0 4px #000, 0 2px 12px rgba(0,0,0,1)'), marginBottom: 6}}>{b.kicker}</div>
			) : null}
			{lines.map((line, n) => (
				<div key={n} style={th ? {fontFamily: th.font, fontWeight: th.weight, fontStyle: th.style, letterSpacing: `${th.spacing}em`, fontSize: fitTheme(line, th, b.size ?? 190, b.fitWidth ?? 910), lineHeight: 1.12, color: th.color, whiteSpace: 'nowrap', textShadow: b.soft ?? th.shadow} : {fontFamily: 'Anton', fontSize: b.fitWidth ? fitTheme(line, {font: 'Anton', weight: 400, spacing: 0}, b.size ?? 190, b.fitWidth) : fitAnton(line, b.size ?? 190), lineHeight: 1.1, color: YELLOW, whiteSpace: 'nowrap', textShadow: b.soft ?? (dark ? 'none' : '0 4px 18px rgba(0,0,0,.7), 0 1px 3px rgba(0,0,0,.6)')}}>
					{line.split(' ').map((word, i) => {
						const delay = counter++ * 3;
						const pop = spring({frame: f - delay, fps: FPS, config: {damping: 13, stiffness: 240, mass: 0.55}});
						// silence fades in, the divine rises slowly, everything else lands with a pop
						const entrance: React.CSSProperties = th?.motion === 'fade'
							? {opacity: interpolate(f, [0, 16], [0, 1], clamp)}
							: th?.motion === 'slam'
							? {opacity: interpolate(f - delay, [0, 1], [0, 1], clamp), transform: `scale(${interpolate(f - delay, [0, 5], [2.6, 1], {...clamp, easing: (x) => x * x})})`}
							: th?.motion === 'bloom'
							? {opacity: interpolate(f - delay, [0, 7], [0, 1], clamp), filter: `blur(${interpolate(f - delay, [0, 8], [16, 0], clamp)}px)`, transform: `scale(${interpolate(f - delay, [0, 14], [1.07, 1], clamp)})`}
							: th?.motion === 'rise'
								? {opacity: interpolate(f - delay, [0, 8], [0, 1], clamp), transform: `translateY(${interpolate(f - delay, [0, 16], [34, 0], {...clamp, easing: (x) => 1 - Math.pow(1 - x, 3)})}px)`}
								: {opacity: f >= delay ? 1 : 0, transform: `scale(${interpolate(pop, [0, 1], [0.72, 1])}) translateY(${interpolate(pop, [0, 1], [26, 0])}px)`};
						// some moods animate each letter on its own
						if (th?.palette || th?.motion === 'type' || th?.motion === 'twinkle') {
							return (
								<span key={i} style={{display: 'inline-block', margin: '0 .11em', whiteSpace: 'nowrap'}}>
									{word.split('').map((ch, c) => {
										const idx = letters++;
										const seed = `${b.start}${n}${i}${c}`;
										let style: React.CSSProperties = {};
										if (th.motion === 'type') style = {opacity: f >= idx * stagger(1.15) ? 1 : 0};
										if (th.motion === 'twinkle') {
											const at = random(`tw${seed}`) * Math.min(12, length * 0.28);
											style = {opacity: interpolate(f, [at, at + 5], [0, 1], clamp) * (0.88 + 0.12 * Math.sin(f / 3 + idx)), transform: `translateY(${interpolate(f, [at, at + 12], [12, 0], clamp)}px)`};
										}
										if (th.motion === 'bounce') {
											const hop = spring({frame: f - idx * stagger(1.3), fps: FPS, config: {damping: 9, stiffness: 260, mass: 0.5}});
											style = {color: th.palette[idx % th.palette.length], opacity: f >= idx * stagger(1.3) ? 1 : 0, transform: `translateY(${interpolate(hop, [0, 1], [70, 0]) + Math.sin((f + idx * 3) / 4.5) * 5}px) rotate(${Math.sin((f + idx * 7) / 7) * 3.5}deg) scale(${interpolate(hop, [0, 1], [0.5, 1])})`};
										}
										if (th.motion === 'burst') {
											const lit = spring({frame: f - idx * stagger(2), fps: FPS, config: {damping: 8, stiffness: 300, mass: 0.45}});
											style = {color: th.palette[(idx + Math.floor(f / 7)) % th.palette.length], opacity: f >= idx * stagger(2) ? 1 : 0, transform: `scale(${interpolate(lit, [0, 1], [2, 1])})`, textShadow: `0 0 ${20 + 14 * Math.sin(f / 3 + idx)}px ${th.palette[idx % th.palette.length]}, 0 3px 8px rgba(0,0,0,.85)`};
										}
										return <span key={c} style={{display: 'inline-block', ...style}}>{ch}</span>;
									})}
								</span>
							);
						}
						const paint = th?.fill ? {backgroundImage: th.fill, WebkitBackgroundClip: 'text', backgroundClip: 'text', color: 'transparent', padding: '0 .03em', filter: th.filter} : {};
						return (
							<span key={i} style={{display: 'inline-block', margin: '0 .11em', ...entrance, ...paint}}>{word}</span>
						);
					})}
				</div>
			))}
		</div>
	);
};

const Grid: React.FC = () => (
	<AbsoluteFill style={{background: '#CFD2CC'}}>
		<AbsoluteFill style={{backgroundImage: 'linear-gradient(rgba(70,110,110,.3) 2px, transparent 2px), linear-gradient(90deg, rgba(70,110,110,.3) 2px, transparent 2px)', backgroundSize: '72px 72px'}} />
		<AbsoluteFill style={{background: 'radial-gradient(circle at 50% 45%, rgba(0,0,0,0) 45%, rgba(0,0,0,.42) 100%)'}} />
	</AbsoluteFill>
);

// White-framed picture on grid paper; it lands with a small settle and a slight tilt
const FrameBeat: React.FC<{b: Beat; f: number}> = ({b, f}) => {
	const land = spring({frame: f, fps: FPS, config: {damping: 15, stiffness: 170, mass: 0.7}});
	const tilt = (b.tilt ?? -1.6) * land;
	return (
		<AbsoluteFill>
			<Grid />
			{b.under ? (
				<Img src={staticFile(`stills/${b.under}.jpg`)} style={{position: 'absolute', left: 110, top: 372, width: 860, height: 640, objectFit: 'cover', border: '14px solid white', transform: 'rotate(6deg)', boxShadow: '0 16px 40px rgba(0,0,0,.35)'}} />
			) : null}
			<div style={{position: 'absolute', left: 60, top: 470, width: 960, height: 720, background: 'white', padding: 16, boxSizing: 'border-box', boxShadow: '0 22px 60px rgba(0,0,0,.45)', transform: `translateY(${interpolate(land, [0, 1], [760, 0])}px) rotate(${tilt}deg)`}}>
				<div style={{position: 'relative', width: '100%', height: '100%', overflow: 'hidden'}}>
					<Clip b={b} f={f} />
				</div>
			</div>
			<Headline b={b} f={f} top={204} dark />
		</AbsoluteFill>
	);
};

// Picture above, speaker below; the subtitle sits on the seam
const SplitBeat: React.FC<{b: Beat; f: number}> = ({b, f}) => {
	const frame = useCurrentFrame();
	const data = useContext(Story$);
	const open = spring({frame: f, fps: FPS, config: {damping: 16, stiffness: 200, mass: 0.6}});
	// a beat can reframe the speaker: larger, moved to the panel's top edge, and matched to the picture above
	const zoom = b.faceZoom ?? 1;
	return (
		<AbsoluteFill style={{background: 'black'}}>
			<div style={{position: 'absolute', left: 0, top: 960, width: 1080, height: 960, overflow: 'hidden'}}>
				<Img src={staticFile(`seq/base/${pad(frame)}.jpg`)} style={{position: 'absolute', left: (b.faceX ?? 540) - 540 * zoom, top: -(b.faceLift ?? data.splitLift), width: 1080 * zoom, height: 1920 * zoom, filter: b.faceFilter}} />
			</div>
			<div style={{position: 'absolute', left: 0, top: interpolate(open, [0, 1], [-960, 0]), width: 1080, height: 960, overflow: 'hidden', borderBottom: `8px solid ${YELLOW}`, boxSizing: 'border-box'}}>
				<Clip b={b} f={f} />
			</div>
			<Headline b={b} f={f} top={214} />
		</AbsoluteFill>
	);
};

// One full picture that divides in two when the second half of a comparison is spoken
const VersusBeat: React.FC<{b: Beat; f: number; second: number}> = ({b, f, second}) => {
	const open = spring({frame: f - second, fps: FPS, config: {damping: 16, stiffness: 170, mass: 0.7}});
	const lower = {...b, seq: b.seq2, frames: b.frames2, mood: 'now', focusY: b.focusY2, motion: undefined, start: b.secondAt, end: b.end};
	return (
		<AbsoluteFill style={{background: 'black'}}>
			<div style={{position: 'absolute', left: 0, top: 0, width: 1080, height: interpolate(open, [0, 1], [1920, 960]), overflow: 'hidden'}}>
				{b.wide ? <WideBeat b={b} f={f} /> : <Clip b={b} f={f} />}
			</div>
			{f >= second ? (
				<div style={{position: 'absolute', left: 0, top: interpolate(open, [0, 1], [1920, 960]), width: 1080, height: 960, overflow: 'hidden', borderTop: `8px solid ${YELLOW}`, boxSizing: 'border-box'}}>
					<Clip b={lower} f={f - second} />
				</div>
			) : null}
			<Headline b={{...b, hold: true, kicker: undefined}} f={f} top={214} />
			{f >= second ? <Headline b={{...b, hold: true, kicker: undefined, text: b.text2, theme: b.theme2 ?? b.theme}} f={f - second} top={1150} /> : null}
		</AbsoluteFill>
	);
};

// Landscape footage kept whole: the picture in the middle, a blurred copy of it filling the frame
const WideBeat: React.FC<{b: Beat; f: number}> = ({b, f}) => {
	const src = staticFile(`seq/${b.seq}/${pad(Math.min(f, (b.frames ?? 1) - 1))}.jpg`);
	const top = b.wideTop ?? 660;
	return (
		<AbsoluteFill style={{background: 'black'}}>
			<Img src={src} style={{position: 'absolute', inset: 0, width: '100%', height: '100%', objectFit: 'cover', transform: 'scale(1.15)', filter: 'blur(34px) brightness(.38) saturate(1.1)'}} />
			<div style={{position: 'absolute', left: 0, top, width: 1080, height: 608, overflow: 'hidden', boxShadow: '0 22px 60px rgba(0,0,0,.65)'}}>
				<Clip b={b} f={f} />
			</div>
		</AbsoluteFill>
	);
};

// Tilted wall of the story's moments, drifting in opposite directions
const CollageBeat: React.FC<{b: Beat; f: number}> = ({b, f}) => {
	const data = useContext(Story$);
	return (
	<AbsoluteFill style={{background: '#111'}}>
		<div style={{position: 'absolute', left: -220, top: -360, width: 1520, height: 2640, transform: 'rotate(-9deg)', display: 'flex', gap: 22}}>
			{[0, 1, 2].map((col) => (
				<div key={col} style={{flex: 1, display: 'flex', flexDirection: 'column', gap: 22, transform: `translateY(${(col % 2 ? -1 : 1) * (f * 7 - 80)}px)`}}>
					{data.collage.filter((_: string, i: number) => i % 3 === col).concat(data.collage.filter((_: string, i: number) => i % 3 === (col + 1) % 3)).map((name: string, i: number) => (
						<Img key={i} src={staticFile(`stills/${name}.jpg`)} style={{width: '100%', height: 640, objectFit: 'cover', borderRadius: 18}} />
					))}
				</div>
			))}
		</div>
		<AbsoluteFill style={{background: 'radial-gradient(circle at 50% 42%, rgba(0,0,0,.72), rgba(0,0,0,.12) 72%)'}} />
		<Headline b={b} f={f} top={560} />
	</AbsoluteFill>
	);
};

// Cream call-to-action card: script word, then stacked capitals that fill the width
const CreamBeat: React.FC<{b: Beat; f: number}> = ({b, f}) => (
	<AbsoluteFill style={{background: CREAM}}>
		<Headline b={{...b, hold: true}} f={f} top={b.top ?? 470} dark />
	</AbsoluteFill>
);

const SUB_SIZE = 56;
const subLines = (text: string): string[] => {
	if (text.length * SUB_SIZE * 0.42 <= 860) return [text];
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

const lines_count = (text: string) => subLines(text).length;

const Subtitle: React.FC<{t: number; beat?: Beat; split: boolean}> = ({t, beat, split}) => {
	const data = useContext(Story$);
	const p = data.phrases.find((x: any) => t >= x.start && t < x.end);
	if (!p || (beat && beat.noSub)) return null;
	const onPaper = beat && (beat.type === 'frame' || beat.type === 'cream');
	const seam = split;
	// a reframed speaker fills their panel to the top edge, so the subtitle strip moves up off the forehead
	const seamTop = beat?.faceZoom ? 952 - (lines_count(p.text) * 63 + 30) : 960;
	const plated = data.subPlate && beat && beat.type === 'head';
	const lines = subLines(p.text);
	const themed = THEMES[split && beat?.theme2 ? beat.theme2 : beat?.theme]?.sub;
	const shadow = '0 0 2px #000, 0 1px 2px #000, 1px 2px 3px rgba(0,0,0,.95), 0 0 9px rgba(0,0,0,.75)';
	return (
		<>
		{seam ? <div style={{position: 'absolute', left: 0, top: seamTop, width: 1080, height: lines.length * 63 + 30, background: 'rgba(0,0,0,.82)'}} /> : null}
		<div style={{position: 'absolute', left: 60, width: 900, textAlign: 'center', fontFamily: 'Crimson', fontSize: SUB_SIZE, lineHeight: 1.12, color: onPaper ? INK : themed ?? '#F7D94C', textShadow: onPaper ? 'none' : shadow, ...(seam ? {top: seamTop + 14} : beat && beat.type === 'frame' ? {top: 1250} : {bottom: 480})}}>
			{lines.map((line, i) => (
				<div key={i} style={{whiteSpace: 'nowrap'}}>
					<span style={plated ? (typeof data.subPlate === 'number'
						? {background: `rgba(12,8,4,${data.subPlate})`, boxShadow: `0 0 16px 10px rgba(12,8,4,${data.subPlate * 0.9})`, padding: '0 10px', borderRadius: 18, boxDecorationBreak: 'clone'}
						: {background: 'rgba(12,8,4,.74)', padding: '2px 18px', borderRadius: 12, boxDecorationBreak: 'clone'}) : undefined}>{line}</span>
				</div>
			))}
		</div>
		</>
	);
};

// Warm light sweeping across a transition, as in reel5's move into its cream card
const Leak: React.FC<{f: number}> = ({f}) => {
	const p = interpolate(f, [0, 9], [0, 1], clamp);
	return <AbsoluteFill style={{mixBlendMode: 'screen', opacity: interpolate(p, [0, 0.35, 1], [0, 0.95, 0]), background: `radial-gradient(ellipse 70% 90% at ${interpolate(p, [0, 1], [-10, 110])}% 50%, #FFD27A 0%, #FF8A2A 38%, rgba(0,0,0,0) 72%)`}} />;
};

export const Story: React.FC<{data: any}> = ({data}) => {
	const ready = useFonts();
	const frame = useCurrentFrame();
	const t = frame / FPS;
	// whole frames, the same rounding as extract_frames.py, so every frame asked for exists
	const b = data.beats.find((x: any) => frame >= Math.round(x.start * FPS) && frame < Math.round(x.end * FPS));
	const f = b ? frame - Math.round(b.start * FPS) : 0;
	const coversBase = b && (b.type === 'full' || b.type === 'frame' || b.type === 'collage' || b.type === 'cream' || b.type === 'split' || b.type === 'versus' || b.type === 'wide');
	const second = b && b.type === 'versus' ? Math.round((b.secondAt - b.start) * FPS) : 0;
	const split = !!b && (b.type === 'split' || (b.type === 'versus' && f >= second));
	// a quick settle on speaker beats, so the cut lands; protected beats stay still
	const faceFilter = b?.mood === 'low' ? 'saturate(.22) contrast(1.06) brightness(.9)' : b?.mood === 'warm' ? 'sepia(.14) saturate(1.1) brightness(1.02)' : undefined;
	const hit = (at: number, length: number) => (f - at) / length;
	const moving: React.CSSProperties = {};
	if (b?.enter === 'whip' && f < 6) {
		moving.transform = `translateX(${Math.pow(1 - hit(0, 6), 2) * 300}px)`;
		moving.filter = `blur(${(1 - hit(0, 6)) * 20}px)`;
	}
	if (b?.shake && f < 15) {
		const force = (1 - f / 15) * 26;
		moving.transform = `translate(${(random(`shx${frame}`) - 0.5) * force}px, ${(random(`shy${frame}`) - 0.5) * force}px) scale(1.03)`;
	}
	// a held pause: the last frame before it stays on screen with a slow push, so no footage repeats
	const hold0 = b?.freeze ? Math.round(b.freeze[0] * FPS) : 0;
	const hold1 = b?.freeze ? Math.round(b.freeze[1] * FPS) : 0;
	const held = !!b?.freeze && frame >= hold0 && frame < hold1;
	const settle = b && b.type === 'head' && !b.calm ? interpolate(f, [0, 7], [1.045, 1], {...clamp, easing: (x) => 1 - Math.pow(1 - x, 3)}) : 1;
	const leak = data.beats.find((x: any) => x.leak && frame >= Math.round(x.start * FPS) - 4 && frame < Math.round(x.start * FPS) + 6);
	if (!ready) return <AbsoluteFill style={{background: 'black'}} />;
	return (
		<Story$.Provider value={data}>
		<AbsoluteFill style={{background: 'black'}}>
			<AbsoluteFill style={moving}>
			{coversBase ? null : <Img src={staticFile(`seq/base/${pad(held ? hold0 - 1 : frame)}.jpg`)} style={{width: '100%', height: '100%', transform: `scale(${settle * (held ? 1 + 0.05 * (frame - hold0) / Math.max(hold1 - hold0, 1) : 1)})`, filter: faceFilter}} />}
			{b && b.type === 'full' ? <AbsoluteFill><Clip b={b} f={f} /><AbsoluteFill style={{background: 'linear-gradient(rgba(0,0,0,.42), rgba(0,0,0,0) 34%, rgba(0,0,0,0) 56%, rgba(0,0,0,.72) 80%, rgba(0,0,0,.55))'}} /></AbsoluteFill> : null}
			{b && b.type === 'frame' ? <FrameBeat b={b} f={f} /> : null}
			{b && b.type === 'split' ? <SplitBeat b={b} f={f} /> : null}
			{b && b.type === 'collage' ? <CollageBeat b={b} f={f} /> : null}
			{b && b.type === 'cream' ? <CreamBeat b={b} f={f} /> : null}
			{b && b.type === 'versus' ? <VersusBeat b={b} f={f} second={second} /> : null}
				{b && b.type === 'wide' ? <WideBeat b={b} f={f} /> : null}
			{b && b.dust ? <Dust f={f} /> : null}
			{b && b.stars ? <Stars f={f} near={b.starsNear} /> : null}
			{b && b.petals ? <Petals f={f} /> : null}
			{b && b.rockets ? <Rockets f={f} seed={b.rockets} /> : null}
			{b && (b.type === 'full' || b.type === 'wide') ? <Headline b={b} f={f} top={b.top} /> : null}
			{b && b.type === 'head' ? <Headline b={b} f={f} /> : null}
			{b && b.sparks ? <Sparks f={f} seed={b.sparks} y={b.sparkY ?? 400} /> : null}
			</AbsoluteFill>
			{b && b.flash && f < 5 ? <AbsoluteFill style={{background: 'white', opacity: interpolate(f, [0, 5], [0.85, 0], clamp)}} /> : null}
			{leak ? <Leak f={frame - (Math.round(leak.start * FPS) - 4)} /> : null}
			<Subtitle t={t} beat={b} split={split} />
		</AbsoluteFill>
		</Story$.Provider>
	);
};
