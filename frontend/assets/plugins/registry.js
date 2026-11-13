/* Only bundled contributions register here; manifests select by stable ID. */
import {mountGitHubCli} from './github-cli.js';

const contributions = new Map([['github-cli', mountGitHubCli]]);
export function pluginContribution(id) { return contributions.get(id); }
