#!/usr/bin/env python3
"""Offline v0.7.1 -> v0.7.2 migration test, no API/network/orders."""
import importlib.util,json,tempfile,shutil,os
from pathlib import Path

src=Path(__file__).with_name('okx_bot072.py')
with tempfile.TemporaryDirectory() as temp:
    p=Path(temp)
    for name in ('state','logs'):(p/name).mkdir()
    cfg=dict(instrument='ETH-USDT-SWAP',direction='long',td_mode='cross',
        buy_type='post_only',tp_type='attached_limit',grid_step='.50',take_profit='2.75',
        order_size='0.03',active_buy_orders=25,max_grids=500,max_exchange_orders=500,
        active_tp_orders=25,contract_value='.1',tick_size='.01',contract_lot='.01',
        contract_min='.01',maker_fee_at_setup='.0002',upper_price='2800',
        capital_reference_price='2800',buy_base_qty='0.003',live_trading=False)
    (p/'config.json').write_text(json.dumps(cfg))
    (p/'.env').write_text('OKX_API_KEY=OFFLINE\nOKX_API_SECRET=OFFLINE\nOKX_API_PASSPHRASE=OFFLINE\n')
    shutil.copy2(src,p/src.name)
    old={'version':'0.7.1','instrument':'ETH-USDT-SWAP','completed':1205,
         'realized_usdt':'6.36345975','cycles':{},'aggregate_exits':[
             {'clOrdId':'UNRESOLVED-MARKET','status':'PENDING',
              'parentCycleIds':['SAMPLE-01']}],
         'escape_collection':{'startedAt':12345,'cycles':['SAMPLE-01']},
         'archived_cycles':[]}
    for i in range(251):
        state='BUY_LIVE' if i<25 else ('TP_WAIT' if i<50 else (
            'VIRTUAL_TP' if i<201 else 'CANCELED'))
        old['cycles'][f'{2200+i/2:.2f}']={'state':state,'buyPx':f'{2200+i/2:.2f}',
            'tpPx':f'{2202.75+i/2:.2f}','ordId':'FAKE-ORDER-'+str(i),
            'cycleId':f'ETH-SAMPLE-{i}','filledBuy':'0.03' if state in ('TP_WAIT','VIRTUAL_TP') else '0',
            'filledTP':'0','tpOrders':[],'virtualTpCrossed': i%2==0}
    before=p/'state'/'bot04_state.json'
    before.write_text(json.dumps(old))
    os.environ['OKX_BOT04_AUDIT']='READ_ONLY'
    spec=importlib.util.spec_from_file_location('offline_eth_v072',p/src.name)
    mod=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.VER=='0.7.2'
    upgraded=mod.ensure_state()
    expected=old|{'version':'0.7.2'}
    assert upgraded==expected,'Trading state payload changed during migration'
    assert json.loads(before.read_text())==expected
    bak=p/'state'/'bot04_state_before_v072.json'
    assert json.loads(bak.read_text())==old
    assert mod.ensure_state()==expected
    assert len(upgraded['cycles'])==251
    assert not list((p/'state').glob('*.sqlite'))
    print('✅ ETH state 0.7.1->0.7.2: 251 cycles, TP links,  aggregate intents, price-cross latches preserved; NO API')
