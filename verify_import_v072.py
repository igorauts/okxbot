#!/usr/bin/env python3
"""Full-module import and v0.7.0 -> v0.7.2 migration offline; no exchange requests."""
import importlib.util, tempfile, json, shutil, os
from pathlib import Path

p=Path(__file__).with_name('okx_bot072.py')
with tempfile.TemporaryDirectory() as temp:
    root=Path(temp)
    for n in ('state','logs'): (root/n).mkdir()
    cfg=dict(instrument='ETH-USDT-SWAP', direction='long',td_mode='cross',
       buy_type='post_only',tp_type='attached_limit',grid_step='.50',take_profit='2.75',
       order_size='1',active_buy_orders=25,max_grids=500,max_exchange_orders=500,
       active_tp_orders=74,contract_value='.01',tick_size='.01',contract_lot='.01',
       contract_min='.01',maker_fee_at_setup='.0002',upper_price='2800',
       capital_reference_price='2800',buy_base_qty='.01',live_trading=False,
       created_at=1700000000,name='mock0610')
    (root/'config.json').write_text(json.dumps(cfg))
    (root/'.env').write_text('OKX_API_KEY=NOT_REAL\nOKX_API_SECRET=NOT_REAL\nOKX_API_PASSPHRASE=NOT_REAL\n')
    shutil.copy2(p,root/p.name)
    old=dict(version='0.7.0',instrument='ETH-USDT-SWAP',cycles={},
             archived_cycles=[],completed=23,bootstrapped=True,started_at=1700000000)
    state_file=root/'state'/'bot04_state.json'
    state_file.write_text(json.dumps(old))
    os.environ['OKX_BOT04_AUDIT']='READ_ONLY'
    spec=importlib.util.spec_from_file_location('okx_v070_offline',root/'okx_bot072.py')
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    assert m.VER=='0.7.2'
    m.run_tests()
    upgraded=m.ensure_state()
    assert upgraded['version']=='0.7.2' and upgraded['completed']==23
    backup=root/'state'/'bot04_state_before_v071.json'
    assert json.loads(backup.read_text())==old
    assert m.ensure_state()['version']=='0.7.2'
    assert not list((root/'state').glob('*.sqlite'))
    print('PASS full module import, grid tests, v070->v072 migration and backup; NO network/orders')
