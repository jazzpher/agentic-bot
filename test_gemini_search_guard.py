"""Offline regressions: no model keys, external search, Docker or browser needed."""
import copy
import json
from unittest.mock import patch

from agenticbot import agent, config, providers
from agenticbot.tools import ToolError, ToolResult
from agenticbot.tools import web
from test_tool_order_truncation import (LoopTestCase, FakeSSEResponse, final,
                                        scripted, sse, tool_batch)


class GeminiSignatureTests(LoopTestCase):
    def stream_events(self, *chunks):
        with patch('agenticbot.providers.requests.post', return_value=FakeSSEResponse(sse(*chunks))):
            return list(providers.stream_chat(self.cfg, []))

    def test_late_metadata_is_kept_per_call_and_nested_fields_merged(self):
        events = self.stream_events(
            {'choices': [{'delta': {'tool_calls': [
                {'index': 0, 'id': 'a', 'function': {'name': 'calculate', 'arguments': '{"expression":'},
                 'extra_content': {'google': {'other': 'keep'}}},
                {'index': 1, 'id': 'b', 'function': {'name': 'current_time', 'arguments': '{}'}}]}}]},
            {'choices': [{'delta': {'tool_calls': [
                {'index': 0, 'function': {'arguments': '"1+1"}'},
                 'extra_content': {'google': {'thought_signature': 'opaque+/=signature-a'}}},
                {'index': 1, 'thought_signature': 'signature-b'}]}, 'finish_reason': 'tool_calls'}]})
        calls = next(e['calls'] for e in events if e['type'] == 'tool_calls')
        self.assertEqual(json.loads(calls[0]['arguments']), {'expression': '1+1'})
        self.assertEqual(calls[0]['extra_content'], {'google': {
            'other': 'keep', 'thought_signature': 'opaque+/=signature-a'}})
        self.assertEqual(calls[1]['thought_signature'], 'signature-b')
        self.assertNotIn('extra_content', calls[1])
        self.assertNotIn('thought_signature', calls[0])

    def test_full_message_proxy_preserves_metadata(self):
        events = self.stream_events({'choices': [{'message': {'role': 'assistant', 'tool_calls': [
            {'id': 'full', 'function': {'name': 'current_time', 'arguments': {}},
             'extra_content': {'google': {'thought_signature': 'full-signature'}}}]},
            'finish_reason': 'tool_calls'}]})
        call = next(e['calls'][0] for e in events if e['type'] == 'tool_calls')
        self.assertEqual(call['extra_content']['google']['thought_signature'], 'full-signature')
        self.assertEqual(call['arguments'], '{}')

    def test_signature_round_trips_into_next_actual_http_payload_and_history(self):
        body = sse({'choices': [{'delta': {'tool_calls': [
            {'index': 0, 'id': 'signed', 'function': {'name': 'current_time', 'arguments': '{}'},
             'extra_content': {'google': {'thought_signature': 'original-signature'}}}]},
            'finish_reason': 'tool_calls'}]})
        done = sse({'choices': [{'delta': {'content': 'done'}, 'finish_reason': 'stop'}]})
        with patch('agenticbot.providers.requests.post', side_effect=[FakeSSEResponse(body), FakeSSEResponse(done)]) as post:
            result = self.turn()
        self.assertEqual(result.status, 'completed')
        payload = post.call_args_list[1].kwargs['json']
        stored = next(m for m in result.messages if m.get('tool_calls'))
        replay = next(m for m in payload['messages'] if m.get('tool_calls'))
        self.assertEqual(stored['tool_calls'], replay['tool_calls'])
        self.assertEqual(replay['tool_calls'][0]['extra_content']['google']['thought_signature'], 'original-signature')
        self.assertTrue(any(m.get('tool_call_id') == 'signed' for m in payload['messages']))

    def test_metadata_is_deep_copied_and_other_providers_are_unchanged(self):
        call = {'id': 'x', 'name': 'current_time', 'arguments': '{}'}
        self.assertEqual(agent._assistant_tool_call(call), {
            'id': 'x', 'type': 'function', 'function': {'name': 'current_time', 'arguments': '{}'}})
        call['extra_content'] = {'google': {'thought_signature': 'keep'}}
        result = agent._assistant_tool_call(call)
        call['extra_content']['google']['thought_signature'] = 'changed'
        self.assertEqual(result['extra_content']['google']['thought_signature'], 'keep')


class SearchFeedbackTests(LoopTestCase):
    def fake_page(self, text, status=200):
        res = FakeSSEResponse('')
        res.text = text
        res.status_code = status
        return patch.object(web.requests, 'get', return_value=res)

    def test_ddg_snippets_entities_redirects_attribute_order_and_limit(self):
        html = ('<a href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fweather" '
                'class="result__a"><b>Lucena</b> &amp; weather</a>'
                '<a class="result__snippet">Hot &amp; <b>humid</b>.</a>'
                '<a class="result__a" href="https://other.test">Other</a>'
                '<td class="result__snippet">Second snippet.</td>')
        with self.fake_page(html):
            rows = web._search_duckduckgo('lucena weather', 2)
        self.assertEqual(rows, [
            {'title': 'Lucena & weather', 'url': 'https://example.com/weather', 'snippet': 'Hot & humid.'},
            {'title': 'Other', 'url': 'https://other.test', 'snippet': 'Second snippet.'}])
        with self.fake_page(html):
            self.assertEqual(len(web._search_duckduckgo('lucena weather', 1)), 1)

    def test_challenge_is_not_misreported_as_successful_empty_search(self):
        for status, html in [(202, '<html>challenge</html>'), (200, '<form id="challenge-form"></form>')]:
            with self.subTest(status=status), self.fake_page(html, status):
                with self.assertRaisesRegex(ToolError, 'bot challenge'):
                    web._search_duckduckgo('lucena weather', 6)

    def test_empty_search_is_failure_with_actionable_feedback(self):
        with patch.object(web, 'search_backend', return_value='duckduckgo'), patch.dict(web.SEARCHERS, {'duckduckgo': lambda q, n: []}):
            from agenticbot.tools import ToolContext
            result = self.registry.execute(ToolContext(workspace=self.ws), 'web_search', {'query': 'nothing'})
        self.assertFalse(result.ok)
        self.assertIn('No results from duckduckgo', result.output)
        self.assertIn('fetch_url', result.output)

    def test_search_results_are_fed_back_with_urls_and_snippets(self):
        seen = []
        def stream(cfg, messages, **kw):
            seen.append(copy.deepcopy(messages))
            yield from (tool_batch(('s', 'web_search', {'query': 'lucena weather'})) if len(seen) == 1 else final('answer'))
        with patch.object(web, 'search_backend', return_value='duckduckgo'), patch.dict(web.SEARCHERS, {'duckduckgo': lambda q, n: [
                {'title': 'Weather', 'url': 'https://example.com/weather', 'snippet': 'Forecast details'}]}):
            result = self.turn(stream)
        self.assertEqual(result.status, 'completed')
        tool = next(m for m in seen[1] if m.get('role') == 'tool')
        self.assertIn('https://example.com/weather', tool['content'])
        self.assertIn('Forecast details', tool['content'])


class LoopGuardTests(LoopTestCase):
    def search_result(self, ctx, name, args):
        return ToolResult(name, args, 'Search backend: fake\n[1] Weather\nhttps://example.com', True, 0.0)

    def test_near_identical_queries_block_before_execution_and_model_can_answer(self):
        queries = ['Lucena weather today', 'TODAY, weather lucena!', 'weather today Lucena']
        script = [tool_batch((str(i), 'web_search', {'query': q})) for i, q in enumerate(queries)] + [final('using the results')]
        with patch.object(self.registry, 'execute', side_effect=self.search_result) as execute:
            result = self.turn(scripted(script))
        self.assertEqual(result.status, 'completed')
        self.assertEqual(execute.call_count, 2)
        denied = next(m for m in result.messages if m.get('tool_call_id') == '2')
        self.assertIn('Repeated near-identical search blocked', denied['content'])

    def test_persistent_repeat_is_bounded_and_never_completed(self):
        with patch.object(self.registry, 'execute', side_effect=self.search_result) as execute:
            result = self.turn(scripted([tool_batch(('s', 'web_search', {'query': 'lucena weather'}))]))
        self.assertEqual(execute.call_count, 2)
        self.assertEqual(result.status, 'error')
        self.assertEqual(result.steps, 4)
        self.assertTrue(any(e['type'] == 'error' and 'quota' in e['text'] for e in self.events))

    def test_distinct_query_budget_counts_calls_not_steps_and_pairs_every_result(self):
        queries = ['weather', 'python', 'elections', 'economy']
        batch = tool_batch(*[(str(i), 'web_search', {'query': q}) for i, q in enumerate(queries)])
        with patch.object(config, 'MAX_SEARCHES_PER_TURN', 2), patch.object(self.registry, 'execute', side_effect=self.search_result) as execute:
            result = self.turn(scripted([batch]))
        self.assertEqual(execute.call_count, 2)
        self.assertEqual(result.status, 'error')
        self.assertEqual(result.steps, 1)
        self.assertEqual(len([m for m in result.messages if m.get('role') == 'tool']), 4)

    def test_general_budget_cannot_be_bypassed_by_a_parallel_batch(self):
        batch = tool_batch(*[(str(i), 'read_file', {'path': str(i)}) for i in range(5)])
        with patch.object(config, 'MAX_TOOL_CALLS_PER_TURN', 2), patch.object(self.registry, 'execute', side_effect=self.search_result) as execute:
            result = self.turn(scripted([batch]))
        self.assertEqual(execute.call_count, 2)
        self.assertEqual(result.status, 'error')
        self.assertEqual(len([m for m in result.messages if m.get('role') == 'tool']), 5)

    def test_exact_read_guard_does_not_conflate_paths_and_resets_on_write(self):
        guard = agent._TurnToolGuard()
        def reserve(name, **args):
            return guard.reserve({'name': name, 'arguments': json.dumps(args)}, self.registry)
        for i in range(3):
            self.assertIsNone(reserve('read_file', path='A.txt'))
        self.assertIn('Repeated identical', reserve('read_file', path='A.txt'))
        self.assertIsNone(reserve('read_file', path='a.txt'))
        self.assertIsNone(reserve('write_file', path='A.txt', content='new'))
        self.assertIsNone(reserve('read_file', path='A.txt'))

    def test_guard_resets_for_a_new_turn_and_distinct_queries_are_allowed(self):
        script = [tool_batch(('a', 'web_search', {'query': 'Lucena weather'})),
                  tool_batch(('b', 'web_search', {'query': 'Manila weather'})), final('answer')]
        with patch.object(self.registry, 'execute', side_effect=self.search_result):
            for _ in range(2):
                self.assertEqual(self.turn(scripted(script)).status, 'completed')

    def test_json_protocol_obeys_same_guard(self):
        def stream(cfg, messages, **kw):
            yield {'type': 'text', 'delta': '{"tool":"web_search","arguments":{"query":"lucena weather"}}'}
            yield {'type': 'finish', 'reason': 'stop'}
        from agenticbot.tools import ToolContext
        with patch.object(self.registry, 'execute', side_effect=self.search_result) as execute:
            result = agent.run_turn([{'role': 'user', 'content': 'weather'}], self.registry,
                                    ToolContext(workspace=self.ws), self.cfg, self.events.append,
                                    tool_mode='json', stream_fn=stream)
        self.assertEqual(result.status, 'error')
        self.assertEqual(execute.call_count, 2)
