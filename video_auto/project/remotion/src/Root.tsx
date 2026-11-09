import React from 'react';
import {Composition} from 'remotion';
import {Main} from './Main';
import {Reel5} from './Reel5';
import {Story} from './Story';
import ramayana from './ramayana.json';
import ramayana2 from './ramayana2.json';
import ramayana3 from './ramayana3.json';
import ramayana5 from './ramayana5.json';
import diwali2 from './diwali2.json';
import data from './data.json';
import deepavali from './deepavali.json';

export const Root: React.FC = () => (
	<>
	<Composition id="Diwali2" component={Story} defaultProps={{data: diwali2}} fps={30} width={1080} height={1920} durationInFrames={Math.round(diwali2.total * 30)} />
	<Composition id="Ramayana5" component={Story} defaultProps={{data: ramayana5}} fps={30} width={1080} height={1920} durationInFrames={Math.round(ramayana5.total * 30)} />
	<Composition id="Ramayana3"component={Story} defaultProps={{data: ramayana3}} fps={30} width={1080} height={1920} durationInFrames={Math.round(ramayana3.total * 30)} />
	<Composition id="Ramayana2" component={Story} defaultProps={{data: ramayana2}} fps={30} width={1080} height={1920} durationInFrames={Math.round(ramayana2.total * 30)} />
	<Composition id="Ramayana" component={Story} defaultProps={{data: ramayana}} fps={30} width={1080} height={1920} durationInFrames={Math.round(ramayana.total * 30)} />
	<Composition id="Deepavali" component={Story} defaultProps={{data: deepavali}} fps={30} width={1080} height={1920} durationInFrames={Math.round(deepavali.total * 30)} />
	<Composition id="Reel5" component={Reel5} fps={30} width={1080} height={1920} durationInFrames={Math.round(data.total * 30)} />
	<Composition
		id="Reel"
		component={Main}
		fps={30}
		width={1080}
		height={1920}
		durationInFrames={Math.round(data.total * 30)}
	/>
	</>
);
