import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';

const source = await readFile(process.argv[2], 'utf8');
const model = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));
const task = (seq, title, visibility = 'public') => ({seq, visibility, event_name: 'delta.presentation',
  payload: {kind: 'browser_task', data: {activity_title: title, status: title}}});

assert.equal(model.eventSequence({seq: null}), null);
assert.equal(model.eventRole({payload: {role: 'assistant'}}), 'assistant');
assert.equal(model.eventText({payload: {content: [{type: 'text', text: 'Muse'}, {type: 'text', text: ' reply'}]}}), 'Muse reply');
assert.equal(model.eventIsStreaming({event_name: 'message.stream'}), true);
assert.equal(model.latestBrowserTask([task(1, 'private', 'private'), task(2, 'latest')]).data.activity_title, 'latest');
assert.deepEqual(model.visibleMessages([task(1, 'private', 'private'), task(2, 'public')]).map(event => event.seq), [2]);

const current = model.reduceBrowserActivity(null, [task(10, 'running')]);
assert.equal(model.reduceBrowserActivity(current, [task(9, 'stale')]).data.activity_title, 'running');
assert.equal(model.reduceBrowserActivity(current, [task(10, 'updated')]).data.activity_title, 'updated');
assert.equal(model.reduceBrowserActivity(current, [task(null, 'unordered')], {allowUnordered: false}).data.activity_title, 'running');
assert.equal(model.reduceBrowserActivity(current, [task(null, 'unordered')]).data.activity_title, 'running');
assert.equal(model.reduceBrowserActivity(null, [task(null, 'old')], {allowUnordered: false}), null);
assert.equal(model.reduceBrowserActivity(current, [{...task(10, 'hidden'), visibility: 'private'}]).card, null);
const hidden = model.reduceBrowserActivity(current, [task(11, 'private', 'private')]);
assert.equal(hidden.card, null);
assert.equal(model.reduceBrowserActivity(hidden, [task(9, 'stale')]).card, null);
assert.equal(model.reduceBrowserActivity(current, [task(10, 'same')], {allowEqual: false}).data.activity_title, 'running');
assert.equal(model.reduceBrowserActivity(current, [{seq: 11, role: 'assistant', visibility: 'private', payload: {content: 'secret'}}]).data.activity_title, 'running');
assert.equal(model.browserTaskIsActive([task(10, 'running')]), true);
assert.equal(model.browserTaskIsActive([task(10, 'completed')]), false);

const merged = model.mergeEvents([{seq: 2, message_id: 'two'}, {seq: 1, message_id: 'one'}], [{seq: 2, message_id: 'two', updated: true}]);
assert.deepEqual(merged.map(event => event.seq), [1, 2]);
assert.equal(merged[1].updated, true);
