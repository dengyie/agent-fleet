import assert from 'node:assert/strict';
import {pathToFileURL} from 'node:url';

const [statePath] = process.argv.slice(2);
const {createConversationState, mergeConversationPage} = await import(pathToFileURL(statePath));
const first = mergeConversationPage(createConversationState(), {
  messages: [
    {message_id: 'u2', role: 'user', turn_sequence: 2, content: 'second'},
    {message_id: 'a2', role: 'assistant', turn_sequence: 2, content: 'reply'}
  ],
  runs: [{run_id: 'r2', trigger_message_id: 'u2'}],
  history: {has_more: true, before_sequence: 2},
  context_window: {omitted_turns: 4}
}, false);
const earlier = mergeConversationPage(first, {
  messages: [
    {message_id: 'u1', role: 'user', turn_sequence: 1, content: 'first'},
    {message_id: 'a1', role: 'assistant', turn_sequence: 1, content: 'reply'}
  ],
  runs: [{run_id: 'r1', trigger_message_id: 'u1'}],
  history: {has_more: false, before_sequence: 0}
}, true);
assert.deepEqual(earlier.orderedMessages.map(item => item.message_id), ['u1', 'a1', 'u2', 'a2']);
assert.deepEqual(earlier.orderedRuns.map(item => item.run_id), ['r1', 'r2']);
assert.equal(earlier.history.has_more, false);
assert.equal(earlier.contextWindow.omitted_turns, 4);
assert.equal(first.messages.has('u1'), false, 'page merges must not mutate the previous state');

const refreshed = mergeConversationPage(earlier, {
  messages: [{message_id: 'u4', role: 'user', turn_sequence: 4, content: 'new conversation'}],
  runs: [{run_id: 'r4', trigger_message_id: 'u4'}],
  history: {has_more: false, before_sequence: 4}
}, false);
assert.deepEqual(refreshed.orderedMessages.map(item => item.message_id), ['u4']);
assert.deepEqual(refreshed.orderedRuns.map(item => item.run_id), ['r4']);

const staleHistory = mergeConversationPage(createConversationState(), {
  messages: [{message_id: 'u20', role: 'user', turn_sequence: 20}],
  history: {has_more: true, before_sequence: 1},
  context_window: {omitted_turns: 5}
}, false);
const recovered = mergeConversationPage(staleHistory, {
  messages: [{message_id: 'u40', role: 'user', turn_sequence: 40}],
  history: {has_more: true, before_sequence: 21}
}, false);
assert.deepEqual(recovered.orderedMessages.map(item => item.message_id), ['u40']);
assert.equal(recovered.history.before_sequence, 21, 'gap recovery must replace the stale pagination cursor');
assert.equal(recovered.contextWindow, null, 'gap recovery must clear stale context metadata when the new page omits it');

const updated = mergeConversationPage(refreshed, {
  messages: [{message_id: 'u4', role: 'user', turn_sequence: 4, content: 'updated'}]
}, false);
assert.equal(updated.orderedMessages[0].content, 'updated');
console.log('assistant conversation state passed');
const live = mergeConversationPage(createConversationState(), {runs:[{run_id:'r',state:'running',progress:{sequence:8,segments:[{text:'new'}]}}]},false);
const late = mergeConversationPage(live, {runs:[{run_id:'r',state:'running',progress:{sequence:7,segments:[]}}]},false);
assert.equal(late.runs.get('r').progress.sequence,8,'late poll/history must not erase newer SSE output');
const done = mergeConversationPage(live, {runs:[{run_id:'r',state:'succeeded',result_text:'done',progress:{sequence:8}}]},false);
const stale = mergeConversationPage(done, {runs:[{run_id:'r',state:'running',progress:{sequence:8}}]},false);
assert.equal(stale.runs.get('r').state,'succeeded','late snapshot cannot reopen a terminal run');
