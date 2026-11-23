"""只测手动台的配置、代理兼容和失败报告，不请求中转。"""

import asyncio
import os

from tools import e2e_live as live


def test_load_only_source_llm(tmp_path, monkeypatch):
    source = tmp_path / 'source.yaml'
    source.write_text('llm:\n  base_url: https://relay.invalid/v1\n  api_key: ${TEST_RELAY_KEY}\n'
                      '  models: {fast: not-this-model}\ncomputer: not-a-valid-config\n')
    monkeypatch.setenv('TEST_RELAY_KEY', 'synthetic-example')
    cfg = live.relay_config(source)
    assert cfg.api_key == 'synthetic-example'
    assert cfg.model_for('fast') == cfg.model_for('chat') == live.MODEL


def test_layered_source_only_uses_preferred_chat_connection(tmp_path):
    source = tmp_path / 'source.yaml'
    source.write_text('''llm:
  providers:
    - {name: other, base_url: https://other.invalid/v1, api_key: other-key, models: [other]}
    - {name: relay, base_url: https://relay.invalid/v1, api_key: relay-key, models: [primary]}
  role_pools:
    chat: [{provider: relay, model: primary}, {provider: other, model: other}]
''')
    cfg = live.relay_config(source)
    assert (cfg.base_url, cfg.api_key) == ('https://relay.invalid/v1', 'relay-key')
    assert cfg.models_for('vision') == cfg.models_for('chat') == [live.MODEL]


def test_bracketed_ipv6_proxy_exclusions_keep_other_entries(monkeypatch):
    monkeypatch.setenv('no_proxy', '[::1],::1,[2001:db8::1],.example.org,127.0.0.1')
    live.normalize_no_proxy()
    assert os.environ['no_proxy'] == '::1,2001:db8::1,.example.org,127.0.0.1'


def test_missing_explicit_config_reports_all_failures(tmp_path):
    report = tmp_path / 'report.md'
    assert asyncio.run(live.run(tmp_path / 'absent.yaml', report, range(1, 8))) == 1
    text = report.read_text()
    assert text.count('| FAIL |') == 7
    assert '0 PASS，7 FAIL' in text
    assert str(tmp_path) not in text
