import argparse
import json
import sys
import time
import threading
import urllib.request
import urllib.error
from datetime import datetime, timezone
from typing import Dict, Optional
try:
    from rich.console import Console
    from rich.layout import Layout
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    from rich.align import Align
    from rich import box
    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False
try:
    import curses
    CURSES_AVAILABLE = True
except ImportError:
    CURSES_AVAILABLE = False

class APIClient:

    def __init__(self, base_url: str, store_id: str):
        self.base_url = base_url.rstrip('/')
        self.store_id = store_id

    def _get(self, path: str) -> Optional[Dict]:
        try:
            url = f'{self.base_url}{path}'
            with urllib.request.urlopen(url, timeout=5) as resp:
                return json.loads(resp.read())
        except Exception:
            return None

    def get_metrics(self) -> Optional[Dict]:
        return self._get(f'/stores/{self.store_id}/metrics')

    def get_funnel(self) -> Optional[Dict]:
        return self._get(f'/stores/{self.store_id}/funnel')

    def get_anomalies(self) -> Optional[Dict]:
        return self._get(f'/stores/{self.store_id}/anomalies')

    def get_health(self) -> Optional[Dict]:
        return self._get('/health')

    def get_heatmap(self) -> Optional[Dict]:
        return self._get(f'/stores/{self.store_id}/heatmap')

class RichDashboard:
    REFRESH_INTERVAL = 3

    def __init__(self, api_client: APIClient, store_id: str):
        self.api = api_client
        self.store_id = store_id
        self.console = Console()
        self._metrics = {}
        self._funnel = {}
        self._anomalies = []
        self._health = {}
        self._heatmap = []
        self._last_update = None

    def _fetch_all(self):
        self._metrics = self.api.get_metrics() or {}
        self._funnel = self.api.get_funnel() or {}
        anomalies_resp = self.api.get_anomalies() or {}
        self._anomalies = anomalies_resp.get('active_anomalies', [])
        self._health = self.api.get_health() or {}
        heatmap_resp = self.api.get_heatmap() or {}
        self._heatmap = heatmap_resp.get('cells', [])
        self._last_update = datetime.now(timezone.utc)

    def _make_header(self) -> Panel:
        ts = self._last_update.strftime('%H:%M:%S UTC') if self._last_update else '--:--:--'
        health_status = self._health.get('status', 'unknown')
        status_color = {'healthy': 'green', 'degraded': 'yellow', 'unhealthy': 'red'}.get(health_status, 'white')
        header_text = Text()
        header_text.append('🏪 STORE INTELLIGENCE DASHBOARD\n', style='bold cyan')
        header_text.append(f'  Store: {self.store_id}   ', style='white')
        header_text.append(f'Status: {health_status.upper()}', style=status_color)
        header_text.append(f'   Last Update: {ts}', style='dim')
        return Panel(Align.center(header_text), style='blue')

    def _make_metrics_panel(self) -> Panel:
        m = self._metrics
        if not m:
            return Panel('[yellow]No metrics data — waiting for events...[/yellow]', title='📊 Metrics')
        table = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
        table.add_column('Metric', style='cyan')
        table.add_column('Value', style='bold white')
        table.add_row('👥 Unique Visitors', str(m.get('unique_visitors', 0)))
        conv = m.get('conversion_rate', 0)
        conv_color = 'green' if conv >= 0.5 else 'yellow' if conv >= 0.3 else 'red'
        table.add_row('💰 Conversion Rate', f'[{conv_color}]{conv:.1%}[/{conv_color}]')
        table.add_row('⏱  Avg Dwell Time', f"{m.get('avg_dwell_seconds', 0):.0f}s")
        table.add_row('🧾 Queue Depth', str(m.get('current_queue_depth', 0)))
        abandon = m.get('abandonment_rate', 0)
        table.add_row('🚪 Abandonment Rate', f'{abandon:.1%}')
        table.add_row('💳 Transactions', str(m.get('total_transactions', 0)))
        table.add_row('₹  Total Revenue', f"₹{m.get('total_revenue_inr', 0):,.0f}")
        conf = m.get('data_confidence', 'LOW')
        conf_color = {'HIGH': 'green', 'MEDIUM': 'yellow', 'LOW': 'red'}.get(conf, 'white')
        table.add_row('🎯 Data Confidence', f'[{conf_color}]{conf}[/{conf_color}]')
        return Panel(table, title='📊 Live Metrics', border_style='green')

    def _make_funnel_panel(self) -> Panel:
        f = self._funnel
        if not f:
            return Panel('[yellow]No funnel data yet[/yellow]', title='🔽 Funnel')
        stages = f.get('stages', [])
        table = Table(box=box.SIMPLE_HEAVY)
        table.add_column('Stage', style='cyan')
        table.add_column('Count', justify='right', style='bold')
        table.add_column('Drop-off', justify='right')
        table.add_column('Visual', width=20)
        max_count = max((s['count'] for s in stages), default=1) or 1
        for stage in stages:
            count = stage['count']
            drop = stage['drop_off_pct']
            bar_len = int(count / max_count * 18) if max_count > 0 else 0
            bar = '█' * bar_len + '░' * (18 - bar_len)
            drop_color = 'red' if drop > 50 else 'yellow' if drop > 20 else 'green'
            table.add_row(stage['stage'], str(count), f'[{drop_color}]{drop:.1f}%[/{drop_color}]' if drop > 0 else '-', f'[cyan]{bar}[/cyan]')
        overall = f.get('overall_conversion_rate', 0)
        table.add_row('', '', '', '')
        table.add_row('[bold]Overall Conversion[/bold]', f'[bold green]{overall:.1%}[/bold green]', '', '')
        return Panel(table, title='🔽 Conversion Funnel', border_style='magenta')

    def _make_heatmap_panel(self) -> Panel:
        if not self._heatmap:
            return Panel('[yellow]No zone data yet[/yellow]', title='🗺 Zone Heatmap')
        table = Table(box=box.SIMPLE)
        table.add_column('Zone', style='cyan', width=15)
        table.add_column('Score', justify='right')
        table.add_column('Visits', justify='right')
        table.add_column('Avg Dwell', justify='right')
        table.add_column('Heat', width=20)
        heat_colors = ['blue', 'cyan', 'yellow', 'orange', 'red']
        for cell in sorted(self._heatmap, key=lambda x: x['normalised_score'], reverse=True):
            score = cell['normalised_score']
            color_idx = min(int(score / 20), 4)
            color = heat_colors[color_idx]
            bar_len = int(score / 5)
            bar = '▓' * bar_len + '░' * (20 - bar_len)
            table.add_row(cell['zone_id'], f'{score:.0f}', str(cell['visit_frequency']), f"{cell['avg_dwell_seconds']:.0f}s", f'[{color}]{bar}[/{color}]')
        return Panel(table, title='🗺 Zone Heatmap', border_style='blue')

    def _make_anomalies_panel(self) -> Panel:
        if not self._anomalies:
            return Panel(Align.center(Text('✅ No active anomalies', style='green')), title='⚠️  Anomalies', border_style='green')
        table = Table(box=box.SIMPLE)
        table.add_column('Type', style='bold')
        table.add_column('Severity')
        table.add_column('Zone')
        table.add_column('Description')
        table.add_column('Action')
        severity_colors = {'INFO': 'blue', 'WARN': 'yellow', 'CRITICAL': 'red bold'}
        for a in self._anomalies:
            sev = a.get('severity', 'INFO')
            color = severity_colors.get(sev, 'white')
            table.add_row(a['anomaly_type'], f'[{color}]{sev}[/{color}]', a.get('zone_id') or '—', a['description'][:50] + '...' if len(a['description']) > 50 else a['description'], a['suggested_action'][:40] + '...' if len(a['suggested_action']) > 40 else a['suggested_action'])
        return Panel(table, title=f'⚠️  Active Anomalies ({len(self._anomalies)})', border_style='red')

    def _make_footer(self) -> Panel:
        text = Text()
        text.append(f'  Refreshing every {self.REFRESH_INTERVAL}s  |  ', style='dim')
        text.append('Ctrl+C to exit', style='dim')
        text.append('  |  Store Intelligence API v1.0.0', style='dim')
        return Panel(Align.center(text), style='dim')

    def _build_layout(self) -> Layout:
        layout = Layout()
        layout.split_column(Layout(self._make_header(), size=4), Layout(name='middle'), Layout(self._make_anomalies_panel(), size=12), Layout(self._make_footer(), size=3))
        layout['middle'].split_row(Layout(self._make_metrics_panel()), Layout(name='right'))
        layout['middle']['right'].split_column(Layout(self._make_funnel_panel()), Layout(self._make_heatmap_panel()))
        return layout

    def run(self):
        self.console.print(f'\n[cyan]Connecting to {self.api.base_url}...[/cyan]\n')
        with Live(console=self.console, refresh_per_second=1, screen=True) as live:
            while True:
                try:
                    self._fetch_all()
                    live.update(self._build_layout())
                    time.sleep(self.REFRESH_INTERVAL)
                except KeyboardInterrupt:
                    break

class SimpleDashboard:

    def __init__(self, api_client: APIClient, store_id: str):
        self.api = api_client
        self.store_id = store_id

    def run(self):
        print(f"\n{'=' * 60}")
        print(f'  STORE INTELLIGENCE DASHBOARD — {self.store_id}')
        print(f"{'=' * 60}\n")
        print('Press Ctrl+C to stop\n')
        while True:
            try:
                metrics = self.api.get_metrics() or {}
                funnel = self.api.get_funnel() or {}
                anomalies = (self.api.get_anomalies() or {}).get('active_anomalies', [])
                ts = datetime.now(timezone.utc).strftime('%H:%M:%S UTC')
                print(f'\n[{ts}] Live Store Metrics:')
                print(f"  Unique Visitors:   {metrics.get('unique_visitors', 0)}")
                print(f"  Conversion Rate:   {metrics.get('conversion_rate', 0):.1%}")
                print(f"  Avg Dwell (s):     {metrics.get('avg_dwell_seconds', 0):.0f}")
                print(f"  Queue Depth:       {metrics.get('current_queue_depth', 0)}")
                print(f"  Abandonment Rate:  {metrics.get('abandonment_rate', 0):.1%}")
                print(f"  Revenue (₹):       {metrics.get('total_revenue_inr', 0):,.0f}")
                stages = funnel.get('stages', [])
                if stages:
                    print(f'\n  Funnel:')
                    for s in stages:
                        bar = '█' * int(s['count'] / max(stages[0]['count'], 1) * 20)
                        print(f"    {s['stage']:<15} {s['count']:>4}  {bar}")
                if anomalies:
                    print(f'\n  ⚠️  Active Anomalies ({len(anomalies)}):')
                    for a in anomalies:
                        print(f"    [{a['severity']}] {a['anomaly_type']}: {a['description'][:60]}")
                time.sleep(3)
            except KeyboardInterrupt:
                print('\nDashboard stopped.')
                break

def main():
    parser = argparse.ArgumentParser(description='Store Intelligence Live Dashboard')
    parser.add_argument('--store', default='STORE_BLR_002', help='Store ID')
    parser.add_argument('--api', default='http://localhost:8000', help='API base URL')
    parser.add_argument('--simple', action='store_true', help='Use simple terminal output')
    args = parser.parse_args()
    client = APIClient(args.api, args.store)
    if RICH_AVAILABLE and (not args.simple):
        RichDashboard(client, args.store).run()
    else:
        if not RICH_AVAILABLE:
            print('Rich not installed — using simple dashboard. Run: pip install rich')
        SimpleDashboard(client, args.store).run()
if __name__ == '__main__':
    main()