import React from 'react';
import {interpolate, spring} from 'remotion';

const MINT = '#2EF2C9';
const CYAN = '#49B6FF';
const CREAM = '#FFE9C7';
const FPS = 30;
const clamp = {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'} as const;

type SceneProps = {scene: {kind: string; labels: string[]}; localFrame: number; duration: number};

const pop = (frame: number, delay = 0) => spring({frame: frame - delay, fps: FPS, config: {damping: 12, stiffness: 150}});
const glide = (frame: number, a: number, b: number, from: number, to: number) => interpolate(frame, [a, b], [from, to], clamp);

// mood: -1 sad, 0 neutral, 1 happy
const Person: React.FC<{x: number; y: number; s?: number; color: string; mood?: number; lean?: number; lookAway?: boolean}> = ({x, y, s = 1, color, mood = 0, lean = 0, lookAway}) => {
	const curve = mood * 22;
	return (
		<g transform={`translate(${x} ${y}) scale(${s}) rotate(${lean})`}>
			<path d="M -82 300 Q -86 112 0 104 Q 86 112 82 300 Z" fill={color} />
			<circle cx={0} cy={0} r={82} fill={lookAway ? "#141a26" : CREAM} />
			{lookAway ? null : <path d="M -82 -8 Q -78 -92 0 -90 Q 78 -92 82 -8 Q 46 -48 0 -46 Q -46 -48 -82 -8 Z" fill="#141a26" />}
			{lookAway ? null : (
				<>
					<circle cx={-28} cy={2} r={8} fill="#141a26" />
					<circle cx={28} cy={2} r={8} fill="#141a26" />
					<path d={`M -30 ${36 - curve / 3} Q 0 ${36 + curve} 30 ${36 - curve / 3}`} stroke="#141a26" strokeWidth={7} fill="none" strokeLinecap="round" />
				</>
			)}
		</g>
	);
};

const Heart: React.FC<{x: number; y: number; s: number; o?: number; color?: string}> = ({x, y, s, o = 1, color = MINT}) => (
	<path transform={`translate(${x} ${y}) scale(${s})`} opacity={o} fill={color} d="M 0 30 C -46 -6 -28 -44 0 -22 C 28 -44 46 -6 0 30 Z" />
);

const Word: React.FC<{text: string; top: number; frame: number; delay?: number; size?: number; script?: boolean; color?: string}> = ({text, top, frame, delay = 0, size = 150, script, color = 'white'}) => {
	const fit = Math.min(size, (script ? 860 : 860) / (text.length * (script ? 0.44 : 0.42)));
	return (
		<div style={{position: 'absolute', top, left: 80, width: 860, textAlign: 'center', fontFamily: script ? 'Kaushan' : 'Anton', fontSize: fit, color, lineHeight: 1.05, transform: `scale(${pop(frame, delay)})`, textShadow: `0 0 34px ${color === 'white' ? 'rgba(73,182,255,.65)' : 'rgba(46,242,201,.55)'}`, whiteSpace: 'nowrap'}}>
			{text}
		</div>
	);
};

const Stage: React.FC<{children: React.ReactNode}> = ({children}) => (
	<svg viewBox="0 0 1080 1920" width={1080} height={1920} style={{position: 'absolute', left: 0, top: 0}}>
		{children}
	</svg>
);

const FaceScene: React.FC<SceneProps> = ({scene, localFrame: f, duration}) => {
	const turn = glide(f, duration * 0.42, duration * 0.58, 1, -1);
	return (
		<>
			<Stage>
				<circle cx={540} cy={510} r={190} fill="none" stroke={CYAN} strokeOpacity={0.5} strokeWidth={5} strokeDasharray="12 16" transform={`rotate(${f * 2} 540 510)`} />
				<g transform="translate(540 500) scale(1.5)">
					<circle r={82} fill={CREAM} />
					<circle cx={-28} cy={-8} r={9} fill="#141a26" />
					<circle cx={28} cy={-8} r={9} fill="#141a26" />
					<path d={`M -36 ${30 - turn * 7} Q 0 ${30 + turn * 30} 36 ${30 - turn * 7}`} stroke="#141a26" strokeWidth={8} fill="none" strokeLinecap="round" />
				</g>
			</Stage>
			<Word text={turn > 0 ? scene.labels[0] : scene.labels[1]} top={240} frame={turn > 0 ? f : f - duration * 0.5} size={110} color={turn > 0 ? MINT : 'white'} />
		</>
	);
};

const ArriveScene: React.FC<SceneProps> = ({scene, localFrame: f}) => (
	<>
		<Stage>
			<Person x={380} y={480} color="#7b8aa5" mood={-1} />
			<path d="M 380 310 l 30 -60 l 30 60 z" fill="#ff5d5d" transform={`scale(${pop(f)})`} style={{transformOrigin: '410px 290px'}} />
			<Person x={glide(f, 4, 26, 1200, 690)} y={480} color={MINT} mood={1} lean={glide(f, 4, 26, 12, 0)} />
		</Stage>
		<Word text={scene.labels[0]} top={236} frame={f} delay={18} size={130} color={MINT} />
	</>
);

const MouthScene: React.FC<SceneProps> = ({scene, localFrame: f}) => {
	const zip = glide(f, 3, 18, 0, 1);
	return (
		<>
			<Stage>
				<g transform="translate(540 520) scale(1.65)">
					<circle r={82} fill={CREAM} />
					<circle cx={-28} cy={-12} r={8} fill="#141a26" />
					<circle cx={28} cy={-12} r={8} fill="#141a26" />
					<line x1={-40} x2={-40 + 80 * zip} y1={32} y2={32} stroke="#141a26" strokeWidth={9} strokeLinecap="round" />
					<rect x={-46 + 80 * zip} y={24} width={13} height={18} rx={3} fill={MINT} />
				</g>
			</Stage>
			<Word text={scene.labels[0]} top={246} frame={f} size={78} />
		</>
	);
};

const MeetScene: React.FC<SceneProps> = ({scene, localFrame: f}) => {
	const walk = glide(f, 3, 26, 0, 1);
	return (
		<>
			<Stage>
				<rect x={0} y={1160} width={1080} height={8} fill={CYAN} opacity={0.4} />
				<Person x={interpolate(walk, [0, 1], [90, 400])} y={820} s={1.05} color={CYAN} mood={1} />
				<Person x={interpolate(walk, [0, 1], [990, 680])} y={820} s={1.05} color={MINT} mood={1} />
				<Heart x={540} y={690} s={1.5 * pop(f, 24)} />
			</Stage>
			<Word text={scene.labels[0]} top={360} frame={f} size={170} script />
		</>
	);
};

const SmileScene: React.FC<SceneProps> = ({scene, localFrame: f}) => {
	const friends = [[210, 650], [870, 650], [230, 440], [850, 440]];
	return (
		<>
			<Stage>
				<Person x={540} y={490} s={1.05} color={MINT} mood={1} />
				{friends.map(([x, y], i) => (
					<g key={i} transform={`translate(${x} ${y + Math.sin((f + i * 8) / 6) * 8}) scale(${0.55 * pop(f, 6 + i * 6)})`}>
						<circle r={82} fill={CREAM} />
						<circle cx={-28} cy={0} r={9} fill="#141a26" />
						<circle cx={28} cy={0} r={9} fill="#141a26" />
						<path d="M -32 26 Q 0 66 32 26 Z" fill="#141a26" />
					</g>
				))}
			</Stage>
			<Word text={scene.labels[0]} top={228} frame={f} size={110} script color={MINT} />
		</>
	);
};

const MeterScene: React.FC<SceneProps> = ({scene, localFrame: f}) => {
	const fill = glide(f, 2, 26, 0, 200);
	const shake = fill >= 100 ? Math.sin(f * 2.2) * 4 : 0;
	return (
		<>
			<Word text={`${Math.round(fill)}%`} top={270} frame={f} size={250} color={MINT} />
			<Stage>
				<g transform={`translate(${shake} 0)`}>
					<rect x={120} y={610} width={840} height={72} rx={36} fill="none" stroke="white" strokeWidth={6} />
					<clipPath id="meter"><rect x={132} y={622} width={816} height={48} rx={24} /></clipPath>
					<rect clipPath="url(#meter)" x={132} y={622} width={816 * Math.min(fill / 200, 1)} height={48} fill={MINT} />
					<line x1={540} x2={540} y1={594} y2={700} stroke="white" strokeWidth={5} opacity={0.75} />
				</g>
			</Stage>
			<Word text={scene.labels[0].replace('200% ', '')} top={720} frame={f} delay={8} size={92} />
		</>
	);
};

const StayScene: React.FC<SceneProps> = ({scene, localFrame: f, duration}) => {
	const mood = glide(f, duration * 0.35, duration * 0.85, -1, 1);
	return (
		<>
			<Stage>
				<Person x={390} y={440} color="#7b8aa5" mood={mood} />
				<Person x={glide(f, 0, 16, 900, 680)} y={440} color={MINT} mood={0.6} />
				<path d={`M 640 ${560} Q 530 ${510} 455 ${550}`} stroke={MINT} strokeWidth={36} fill="none" strokeLinecap="round" opacity={glide(f, 12, 22, 0, 1)} />
			</Stage>
			<Word text={scene.labels[0]} top={240} frame={f} size={104} color={MINT} />
		</>
	);
};

const AloneScene: React.FC<SceneProps> = ({localFrame: f}) => (
	<Stage>
		<rect width={1080} height={1920} fill="#040914" />
		<rect x={250} y={250} width={580} height={640} rx={14} fill="#10264a" stroke="#3a5a8c" strokeWidth={12} />
		<line x1={540} x2={540} y1={250} y2={890} stroke="#3a5a8c" strokeWidth={10} />
		<line x1={250} x2={830} y1={570} y2={570} stroke="#3a5a8c" strokeWidth={10} />
		<circle cx={690} cy={390} r={64} fill="#dfe9ff" opacity={0.9} />
		{[[330, 340], [430, 480], [640, 520], [770, 510], [340, 760]].map(([x, y], i) => (
			<circle key={i} cx={x} cy={y} r={4} fill="white" opacity={0.4 + 0.5 * Math.abs(Math.sin((f + i * 7) / 9))} />
		))}
		<Person x={540} y={930} s={1.1} color="#203a63" lookAway lean={glide(f, 0, 40, 0, -3)} />
	</Stage>
);

const PhoneScene: React.FC<SceneProps> = ({scene, localFrame: f}) => (
	<>
		<Word text={scene.labels[0]} top={228} frame={f} size={132} script color={MINT} />
		<div style={{position: 'absolute', left: 300, top: 400, width: 480, height: 430, borderRadius: 52, background: '#0b1526', border: '10px solid #3d5474', boxShadow: '0 0 60px rgba(73,182,255,.5)', transform: `translateY(${(1 - pop(f, 3)) * 200}px)`}}>
			<div style={{position: 'absolute', left: 30, top: 66, padding: '18px 26px', borderRadius: 26, background: CYAN, color: '#04121f', fontFamily: 'Poppins', fontWeight: 700, fontSize: 40, whiteSpace: 'nowrap', transform: `scale(${pop(f, 12)})`, transformOrigin: 'left top'}}>
				{scene.labels[1]}
			</div>
			<div style={{position: 'absolute', right: 34, bottom: 44, width: 70, height: 70, borderRadius: 35, background: MINT, opacity: pop(f, 22)}} />
		</div>
	</>
);

const TeaScene: React.FC<SceneProps> = ({localFrame: f}) => (
	<Stage>
		<rect y={1010} width={1080} height={910} fill="#201107" />
		<rect y={1000} width={1080} height={24} fill="#8a5a33" />
		<Person x={250} y={690} s={1.05} color={CYAN} mood={0.7} />
		<Person x={830} y={690} s={1.05} color={MINT} mood={0.5} />
		{[430, 650].map((x, i) => (
			<g key={i} transform={`translate(${x} 900)`}>
				<path d="M -48 0 L 48 0 L 36 104 L -36 104 Z" fill="#fbf1df" />
				<rect x={-42} y={10} width={84} height={22} fill="#b77a45" />
				{[0, 1].map((j) => (
					<path key={j} d={`M ${-16 + j * 30} -12 q 18 -30 0 -60 q -18 -30 0 -60`} fill="none" stroke="white" strokeWidth={6} strokeLinecap="round" opacity={0.25 + 0.3 * Math.abs(Math.sin((f + j * 12 + i * 6) / 12))} transform={`translate(0 ${-((f + j * 10) % 24)})`} />
				))}
			</g>
		))}
	</Stage>
);

const SunriseScene: React.FC<SceneProps> = ({localFrame: f, duration}) => {
	const rise = glide(f, 0, duration, 0, 1);
	return (
		<Stage>
			<defs>
				<linearGradient id="sky" x1="0" y1="0" x2="0" y2="1">
					<stop offset="0" stopColor="#ff9d3c" />
					<stop offset="0.68" stopColor="#ffd77e" />
				</linearGradient>
			</defs>
			<rect width={1080} height={1920} fill="url(#sky)" />
			<circle cx={540} cy={interpolate(rise, [0, 1], [1000, 620])} r={150} fill="#fff4cf" />
			<path d="M 0 1010 h 150 v -80 h 120 v 60 h 130 v -110 h 150 v 90 h 170 v -70 h 150 v 110 h 210 V 1920 H 0 Z" fill="#54310f" />
			<Person x={440} y={980} s={0.85} color="#2b1b0d" lookAway />
			<Person x={640} y={980} s={0.85} color="#2b1b0d" lookAway />
			<Heart x={540} y={820} s={1.2 * pop(f, 18)} color="#fff" />
		</Stage>
	);
};

const ShieldScene: React.FC<SceneProps> = ({scene, localFrame: f}) => (
	<>
		<Stage>
			<g transform={`translate(540 500) scale(${1.6 * pop(f)})`}>
				<path d="M 0 -130 L 110 -92 L 110 10 Q 100 96 0 138 Q -100 96 -110 10 L -110 -92 Z" fill="none" stroke={MINT} strokeWidth={13} />
				<path d="M -46 2 L -10 40 L 56 -42" stroke="white" strokeWidth={17} strokeLinecap="round" strokeLinejoin="round" fill="none" strokeDasharray="160" strokeDashoffset={interpolate(f, [8, 22], [160, 0], clamp)} />
			</g>
		</Stage>
		<Word text={scene.labels[0]} top={722} frame={f} delay={6} size={105} />
	</>
);

const GiveScene: React.FC<SceneProps> = ({scene, localFrame: f, duration}) => (
	<>
		<Stage>
			<Person x={210} y={510} color={MINT} mood={1} />
			<Person x={870} y={510} color="#7b8aa5" mood={0.1} />
			{[0, 1, 2, 3].map((i) => {
				const local = ((f + i * (duration / 4)) % duration) / duration;
				return <Heart key={i} x={interpolate(local, [0, 1], [290, 790])} y={520 - Math.sin(local * Math.PI) * 130} s={1.05} o={Math.sin(local * Math.PI)} />;
			})}
		</Stage>
		<Word text={scene.labels[0]} top={246} frame={f} size={95} color={MINT} />
	</>
);

const ReceiveScene: React.FC<SceneProps> = ({scene, localFrame: f}) => {
	const rays = pop(f, 16);
	return (
		<>
			<Stage>
				<Person x={540} y={525} s={1} color={MINT} mood={interpolate(f, [0, 22], [0, 1], clamp)} />
				{Array.from({length: 12}).map((_, i) => {
					const a = (i / 12) * Math.PI * 2;
					const d = 150 + rays * 150 + Math.sin(f / 8 + i) * 12;
					return <Heart key={i} x={540 + Math.cos(a) * d} y={520 + Math.sin(a) * d * 0.6} s={0.75 * rays} o={0.92} color={i % 2 ? MINT : '#ffffff'} />;
				})}
			</Stage>
			<Word text={scene.labels[0]} top={232} frame={f} delay={14} size={118} />
		</>
	);
};

const ShareScene: React.FC<SceneProps> = ({scene, localFrame: f, duration}) => {
	const go = glide(f, duration * 0.56, duration * 0.8, 0, 1);
	return (
		<>
			<Stage>
				<rect x={310} y={330} width={360} height={490} rx={46} fill="#0b1526" stroke="#3d5474" strokeWidth={10} />
				<g transform="translate(490 570)">
					<circle cx={-58} r={28} fill={MINT} />
					<circle cx={58} cy={-62} r={28} fill={MINT} />
					<circle cx={58} cy={62} r={28} fill={MINT} />
					<path d="M -58 0 L 58 -62 M -58 0 L 58 62" stroke={MINT} strokeWidth={9} />
				</g>
				<Heart x={interpolate(go, [0, 1], [680, 1040])} y={interpolate(go, [0, 1], [570, 320])} s={1.3} o={go > 0 ? 1 - go * 0.7 : 0} />
			</Stage>
			<Word text={scene.labels[0]} top={228} frame={f} delay={Math.round(duration * 0.56)} size={92} color={MINT} />
		</>
	);
};

const table: Record<string, React.FC<SceneProps>> = {
	face: FaceScene, arrive: ArriveScene, mouth: MouthScene, meet: MeetScene, smile: SmileScene, meter: MeterScene,
	stay: StayScene, alone: AloneScene, phone: PhoneScene, tea: TeaScene, sunrise: SunriseScene, shield: ShieldScene,
	give: GiveScene, receive: ReceiveScene, share: ShareScene,
};

export const SceneArt: React.FC<SceneProps> = (props) => {
	const Art = table[props.scene.kind];
	return Art ? <Art {...props} /> : null;
};
