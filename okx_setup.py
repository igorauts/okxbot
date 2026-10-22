#!/usr/bin/env python3
"""Interactive wizard for NEW OKX USDT linear SWAP bots; GET-only."""
import argparse
import base64
import curses
import hashlib
import hmac
import json
import os
import shutil
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_HALF_UP
from pathlib import Path

from okx_version import VERSION as VER
from okx_contracts import validate_spec
BASE=Path(__file__).resolve().parent
from okx_setup_presets import ASSETS


def D(s):
    try:
        x=Decimal(str(s).replace(',','.').strip())
    except (ValueError,InvalidOperation):
        raise ValueError('Введите число, например 0.25.') from None
    if not x.is_finite():
        raise ValueError('Введите конечное число, например 0.25.')
    return x


def as_string(x):
    return format(x,'f')


def to_tick_up(x,tick):
    if tick<=0:raise ValueError('Некорректный шаг цены OKX')
    return (x/tick).to_integral_value(rounding=ROUND_CEILING)*tick


def fee_break_even(price,maker_commission):
    """Exact ΔP for BUY maker + SELL maker; fee rate is nonnegative cost."""
    if price<=0 or maker_commission<0 or maker_commission>=1:
        raise ValueError('Некорректная цена или комиссия')
    return price*(maker_commission*2)/(1-maker_commission)


def capital(top,step,max_grids,buy_qty):
    low=top-step*(max_grids-1)
    if top<=0 or low<=0:raise ValueError('Нижняя цена полной сетки должна быть > 0.')
    max_position=buy_qty*max_grids
    return low,max_position,max_position*(top+low)/2


def validate_numeric(key,s,ctx):
    if not s.strip(): raise ValueError('Значение не указано. Введите число.')
    x=D(s)
    tick=ctx['tick']; coin=ctx['coin']; lot=ctx['lot']; ct=ctx['ct']
    if key=='step':
        if x<tick:raise ValueError(f'Минимальный GRID STEP: ${tick}.')
        if x%tick:raise ValueError(f'GRID STEP должен быть кратен ${tick}.')
    elif key=='tp':
        min_tp=ctx['min_tp']
        if x<min_tp:raise ValueError(f'Минимальный TP с запасом 20%: ${min_tp}.')
        if x%tick:raise ValueError(f'TP должен быть кратен ${tick}.')
    elif key=='grids':
        if x!=x.to_integral_value() or not 1<=x<=500:
            raise ValueError('Введите целое количество гридов: 1–500.')
    elif key=='qty':
        min_qty=ctx['min_contracts']*ct
        qty_step=lot*ct
        if x<min_qty:raise ValueError(f'Минимальный BUY: {min_qty} {coin}.')
        if (x/ct)%lot:raise ValueError(f'BUY должен быть кратен {qty_step} {coin}.')
    elif key=='buys':
        hi=min(25,int(ctx['grids']))
        if x!=x.to_integral_value() or not 1<=x<=hi:
            raise ValueError(f'Введите целое число следящих BUY: 1–{hi}.')
    elif key=='real_tp':
        if x!=x.to_integral_value() or not 1<=x<=25:
            raise ValueError('Количество базовых реальных TP: 1–25.')
    elif key in ('buy_buf','buy_protect','tp_buf'):
        raise ValueError('Служебные буферы и защита BUY фиксированы.')
    elif key=='upper':
        if x<0:raise ValueError('Верхняя граница не может быть отрицательной.')
        if x and x%tick:raise ValueError(f'Верхняя граница должна быть кратна ${tick}.')
        ref=x if x else ctx['price']
        # Full 1x grid must make mathematical sense from the chosen anchor.
        capital(ref,ctx['step'],int(ctx['grids']),ctx['qty'])
    else:
        raise ValueError('Неизвестное поле')
    return x


def okx_req(path,env=None,params=None):
    params=params or {}
    tail=path+('?' + urllib.parse.urlencode(params) if params else '')
    headers={'User-Agent':'curl/8.5.0','Content-Type':'application/json'}
    if env:
        ts=datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
        sig=base64.b64encode(hmac.new(env['OKX_API_SECRET'].encode(),
            (ts+'GET'+tail).encode(),hashlib.sha256).digest()).decode()
        headers.update({'OK-ACCESS-KEY':env['OKX_API_KEY'],
            'OK-ACCESS-SIGN':sig,'OK-ACCESS-TIMESTAMP':ts,
            'OK-ACCESS-PASSPHRASE':env['OKX_API_PASSPHRASE']})
    req=urllib.request.Request('https://www.okx.com'+tail,headers=headers)
    with urllib.request.urlopen(req,timeout=15) as res:
        obj=json.load(res)
    if obj.get('code')!='0':
        raise RuntimeError(f'OKX API {obj.get("code")}: {obj.get("msg")}')
    return obj.get('data') or []


def read_env(p):
    p=Path(p)
    raw=p.read_text(encoding='utf-8')
    out={}
    for row in raw.splitlines():
        row=row.strip()
        if row and not row.startswith('#') and '=' in row:
            key,value=row.split('=',1)
            out[key.strip()]=value.strip().strip('"').strip("'")
    for key in ('OKX_API_KEY','OKX_API_SECRET','OKX_API_PASSPHRASE'):
        if not out.get(key):raise ValueError(f'.env: отсутствует {key}')
    if out.get('OKX_DEMO','0')!='0':raise ValueError('Включён DEMO. Мастер требует LIVE API.')
    return out,raw


def load_contract(coin,env):
    inst=f'{coin}-USDT-SWAP'
    instruments=okx_req('/api/v5/public/instruments',params={'instType':'SWAP','instId':inst})
    if len(instruments)!=1:raise RuntimeError('Спецификация контракта отсутствует')
    spec=instruments[0]
    try: validate_spec(inst, spec)
    except ValueError as exc: raise RuntimeError(str(exc)) from exc
    tick,ct,lot,minimum=[D(spec[k]) for k in ('tickSz','ctVal','lotSz','minSz')]
    if any(x<=0 for x in (tick,ct,lot,minimum)):
        raise RuntimeError('Некорректные размеры контракта OKX')
    tickers=okx_req('/api/v5/market/ticker',params={'instId':inst})
    if len(tickers)!=1:raise RuntimeError('Текущая цена отсутствует')
    price=D(tickers[0]['bidPx'])
    if price<=0:raise RuntimeError('Неверный BID')
    # For SWAP, OKX fee lookup must be scoped to the instrument family.
    family=spec.get('instFamily') or f'{coin}-USDT'
    fees=okx_req('/api/v5/account/trade-fee',env=env,
                 params={'instType':'SWAP','instFamily':family})
    if len(fees)!=1:raise RuntimeError('Не удалось узнать VIP комиссии')
    fee=fees[0]
    groups=fee.get('feeGroup') or []
    group_id=str(spec.get('groupId') or '')
    if group_id and groups:
        matching=[g for g in groups if str(g.get('groupId'))==group_id]
        if len(matching)!=1:raise RuntimeError('Не удалось найти торговую feeGroup')
        tier=matching[0]
    elif len(groups)==1:
        tier=groups[0]
    elif len(groups)>1:
        # Better to STOP than use an incorrect VIP fee for breakeven.
        raise RuntimeError('OKX returned multiple feeGroups without a matching groupId')
    else:
        tier=fee
    maker_raw=tier.get('maker')
    taker_raw=tier.get('taker')
    if maker_raw in (None,'') or taker_raw in (None,''):
        raise RuntimeError('Отсутствует актуальная VIP комиссия maker/taker')
    # OKX trade-fee convention: NEGATIVE = commission, POSITIVE = rebate.
    maker,taker=-D(maker_raw),-D(taker_raw)
    if maker>=1 or taker>=1:
        raise RuntimeError('Некорректный размер комиссии')
    min_qty=minimum*ct
    break_even=to_tick_up(fee_break_even(price,max(Decimal(0),maker)),tick)
    min_tp=max(tick,to_tick_up(fee_break_even(price,max(Decimal(0),maker))*Decimal('1.2'),tick))
    return {'coin':coin,'instrument':inst,'price':price,'tick':tick,
        'ct':ct,'lot':lot,'min_contracts':minimum,'min_qty':min_qty,
        'maker':maker,'taker':taker,'fee_level':fee.get('level','—'),
        'break_even':break_even,'min_tp':min_tp,'inst_family':family,
        'spec':spec}


def percentage(distance, price):
    if not price or price <= 0:
        return 'н/д'
    return f'{distance / price * 100:.4f}%'


def grid_projection(price, step, levels, qty):
    """Illustration only: fill N equally spaced BUYs below setup BID.

    No upper boundary and no future grid re-anchoring is assumed.
    """
    levels = int(levels)
    if price <= 0 or step <= 0 or qty <= 0 or levels < 1:
        raise ValueError('Недостаточно параметров')
    bottom = price - step * levels
    if bottom <= 0:
        raise ValueError('Расчётная нижняя цена неположительна')
    units = qty * levels
    spent = qty * (price * levels - step * levels * (levels + 1) / 2)
    nominal = units * bottom
    return {'bottom':bottom, 'depth':step*levels/price*100,
            'units':units, 'spent':spent, 'nominal':nominal,
            'floating':nominal-spent}


def tp_reference_rows(ctx):
    bid,tick=ctx['price'],ctx['tick']
    output=[]
    for pct in ('0.08','0.12','0.2','0.3','0.5'):
        distance=to_tick_up(bid*Decimal(pct)/100,tick)
        output.append((pct,distance,percentage(distance,bid)))
    return output


def tp_estimated_net(ctx, distance, qty):
    """Illustrative single fully-filled maker BUY+SELL at displayed BID."""
    bid,fee=ctx['price'],ctx['maker']
    return qty*(distance - fee*bid - fee*(bid+distance))


def signed_usd(value, places=6):
    """Financial display sign BEFORE currency, e.g. +$0.012300."""
    x = D(value)
    if x > 0:
        return f'+${x:,.{places}f}'
    if x < 0:
        return f'−${-x:,.{places}f}'
    return f'$0.{"0" * places}' if places else '$0'


def _suggested_step(ctx):
    return to_tick_up(ctx['price'] * Decimal('0.002'), ctx['tick'])


def lines_for(key, ctx, input_value=''):
    c=ctx['coin'];price=ctx['price'];tick=ctx['tick']
    # Keep every preview row visible before the very first keystroke.
    try:
        candidate=D(input_value) if input_value.strip() else Decimal(0)
    except ValueError:
        candidate=Decimal(0)
    head=[f'Текущий BID: ${as_string(price)} (снимок при открытии мастера)']
    if key=='step':
        return ['Укажите расстояние между GRID-уровнями.', '', *head,
                f'Минимум и шаг: ${as_string(tick)}', '',
                f'GRID STEP: {percentage(candidate,price)} от BID', '',
                f'Например: ${as_string(_suggested_step(ctx))}', '',
                'GRID STEP (USDT):']
    if key=='grids':
        rows=['Укажите количество GRID-уровней (1–500).', '', *head,
              f'GRID STEP: ${as_string(ctx["step"])} ({percentage(ctx["step"],price)})', '']
        if candidate >= 0 and candidate == candidate.to_integral_value():
            bottom=price-ctx['step']*candidate
            rows.extend([f'Покрытие вниз: {percentage(ctx["step"]*candidate,price)}',
                         f'Низ сетки: ${as_string(bottom)}' if bottom>0
                         else 'Низ сетки: ниже нуля — недопустимо', ''])
        else:
            rows.extend(['Покрытие вниз: —', 'Низ сетки: —', ''])
        return rows+['Например: 100', '', 'Количество гридов:']
    if key=='qty':
        rows=['Укажите размер одного BUY-ордера.', '', *head,
              f'GRID: {int(ctx["grids"])} уровней × ${as_string(ctx["step"])}',
              f'Минимум: {as_string(ctx["min_qty"])} {c}',
              f'Шаг количества: {as_string(ctx["lot"]*ctx["ct"])} {c}', '',
              f'Номинал одного ордера при текущем BID: ≈ ${candidate*price:,.4f}', '',
              'РАСЧЁТ ПОЛНОГО ЗАПОЛНЕНИЯ СЕТКИ']
        try:
            if candidate>0:
                proj=grid_projection(price,ctx['step'],ctx['grids'],candidate)
                pct=proj['floating']/proj['spent']*100
            else:
                # Still validate geometry even though an empty amount previews zero.
                grid_projection(price,ctx['step'],ctx['grids'],Decimal(1))
                proj={'units':Decimal(0),'spent':Decimal(0),'nominal':Decimal(0),
                      'floating':Decimal(0)}
                pct=Decimal(0)
            rows += [f'Максимальная позиция: {as_string(proj["units"])} {c}',
                     f'Сумма всех BUY: ${proj["spent"]:,.2f}',
                     f'Номинал на нижней цене: ${proj["nominal"]:,.2f}',
                     f'Плавающий убыток на дне сетки: {signed_usd(proj["floating"],2)} ({pct:.2f}%)', '']
        except ValueError:
            rows.extend(['Диапазон сетки достигает нулевой цены: расчёт невозможен', ''])
        return rows+['Размер BUY ('+c+'):']
    if key=='tp':
        references=tp_reference_rows(ctx)
        width=max(len(as_string(d)) for _,d,_ in references)
        rows=['Укажите расстояние TAKE PROFIT.', '', *head,
              f'Шаг изменения: ${as_string(tick)}', '',
              'Например:', f'{"Цель":<10} {"Шаг":<{width+2}} От BID']
        for pct,distance,actual in references:
            rows.append(f'{pct+"% →":<10} ${as_string(distance):<{width+1}} {actual:>9}')
        net=tp_estimated_net(ctx,candidate,ctx['qty'])
        rows.extend(['',f'TAKE PROFIT: {percentage(candidate,price)} от BID',
                     f'Профит с одной сделки: {signed_usd(net)}', '',
                     'TAKE PROFIT (USDT):'])
        return rows
    if key=='buys':return [
        'Укажите количество следящих BUY LIMIT.', '',
        'Минимум: 1', f'Максимум: {min(25,int(ctx["grids"]))}', '',
        f'Например: {min(25,int(ctx["grids"]))}', '', 'Количество BUY LIMIT:']
    if key=='real_tp':return [
        'Укажите количество реальных TP на бирже.', '',
        'Минимум: 1','Максимум: 25','',
        'Например: 25','', 'Базовых реальных TP:']
    if key=='upper':
        x1=to_tick_up(price*Decimal('1.10'),tick)
        x2=to_tick_up(price*Decimal('1.15'),tick)
        return [
            'Укажите верхнюю границу сетки.', '',
            f'Текущая цена: ${price}',
            f'Ориентир +10–15%: ${x1}–${x2}', '',
            'Можно указать границу НИЖЕ текущей цены.',
            'Тогда новые BUY появятся лишь после снижения BID.',
            '0 — без ограничения.', '',
            f'Например: {x2}', '',
            'Верхняя граница (USDT):']
    raise ValueError('Unknown wizard screen')


def validate_name(raw, existing=()):
    name=raw.strip()
    if not (3<=len(name)<=20):
        raise ValueError('Название: от 3 до 20 символов.')
    if any(not ch.isprintable() or ch in '/\\' for ch in name):
        raise ValueError('Название не должно содержать / или обратную косую черту.')
    if name.casefold() in {x.casefold() for x in existing}:
        raise ValueError('Бот с таким названием уже существует. Выберите другое.')
    return name


def name_input(stdscr,title,existing, initial=''):
    value=str(initial);error=''
    while True:
        y=screen(stdscr,title,[
            'Как назовём бота?', '',
            'По этому имени можно будет смотреть статистику,',
            'запускать и останавливать бота.', '',
            'Например: SOL Grid 1', '',
            'Название бота:'],
            foot='Enter — сохранить имя    Esc — назад',error=error)
        put(stdscr,y,value+'█')
        stdscr.refresh()
        ch=stdscr.get_wch()
        if ch=='\x1b':return None
        if ch in ('\n','\r',curses.KEY_ENTER):
            try:return validate_name(value,existing)
            except ValueError as exc:
                error=str(exc);continue
        if ch in ('\b','\x7f',curses.KEY_BACKSPACE,127):
            value=value[:-1];continue
        if isinstance(ch,str) and ch.isprintable() and len(value)<20:
            value+=ch


def report(ctx):
    c=ctx['coin']; price=ctx['price']; upper=ctx['upper']
    reference=upper if upper else price
    lo,position,cap=capital(reference,ctx['step'],int(ctx['grids']),ctx['qty'])
    profit=tp_estimated_net(ctx,ctx['tp'],ctx['qty'])
    return [
        f'Актив:                         {next((k for k,v in ASSETS.items() if v==c), c)}',
        f'Контракт:                      {ctx["instrument"]}',
        f'Имя бота:                      {ctx.get("name","—")}',
        f'Текущий BID:                   ${price}', '',
        f'GRID STEP:                     ${ctx["step"]} ({percentage(ctx["step"],price)})',
        f'TAKE PROFIT:                   ${ctx["tp"]} ({percentage(ctx["tp"],price)})',
        f'Размер BUY:                    {ctx["qty"]} {c} (≈ ${ctx["qty"]*price:,.2f})',
        f'Профит с одной сделки:        {signed_usd(profit)}', '',
        f'Максимум гридов:               {int(ctx["grids"])}',
        f'Активных BUY LIMIT:            {int(ctx["buys"])} (макс. {int(ctx["buys"])+5})',
        f'Реальных SELL TP:              {int(ctx["real_tp"])} (макс. {int(ctx["real_tp"])+int(ctx["buys"])+5})',
        f'Верхняя граница:               {"Без ограничения" if not upper else "$"+str(upper)}',
        '-----------------------------------------------',
        f'Максимальная позиция:          {position} {c}',
        f'Диапазон полной сетки:        ${reference} → ${lo}',
        f'Расчётный капитал 1x:         ${cap.quantize(Decimal("0.01"),rounding=ROUND_HALF_UP):,.2f}',
        '-----------------------------------------------',
        'LONG  |  CROSS',
    ]


def setup_colors():
    if not curses.has_colors() or os.environ.get('NO_COLOR'):
        return
    try:
        curses.start_color()
        curses.use_default_colors()
        for number, foreground in enumerate((curses.COLOR_CYAN,
                curses.COLOR_GREEN,curses.COLOR_RED,curses.COLOR_WHITE),1):
            curses.init_pair(number,foreground,-1)
    except curses.error:
        pass


def color_of(line):
    if not curses.has_colors() or os.environ.get('NO_COLOR'):
        return 0
    text=str(line)
    if ('убыток' in text.lower() or 'PANIC ALL' in text or 'ниже нуля' in text.lower()
            or 'недопустим' in text.lower()):
        return curses.color_pair(3)|curses.A_BOLD
    if 'Номинал одного ордера' in text:
        return curses.color_pair(2)|curses.A_BOLD
    if ('Покрытие вниз' in text or 'Текущий BID' in text
            or 'от BID' in text or 'Низ сетки' in text):
        return curses.color_pair(1)
    if text.startswith(('РАСЧЁТ ', 'GRID STEP (','TAKE PROFIT (','Размер BUY (')):
        return curses.color_pair(4)|curses.A_BOLD
    return 0


def put(stdscr,y,text,attribute=0):
    height,width=stdscr.getmaxyx()
    if not (0<=y<height and width>4):
        return
    text=str(text)
    try:
        if (text.lstrip().startswith('Профит с одной сделки:')
                and not attribute and curses.has_colors()
                and not os.environ.get('NO_COLOR')):
            i=text.index(':')+1
            # Label stays neutral; color ONLY the signed profit amount.
            prefix=text[:i]
            rest=text[i:]
            stdscr.addnstr(y,2,prefix,max(0,width-4))
            if len(prefix)<width-4:
                color=(curses.color_pair(3) if '−$' in rest or '-$' in rest
                       else curses.color_pair(2))|curses.A_BOLD
                stdscr.addnstr(y,2+len(prefix),rest,max(0,width-4-len(prefix)),color)
            return
        stdscr.addnstr(y,2,text,max(0,width-4),attribute or color_of(text))
    except curses.error:
        pass


def screen(stdscr,title,body,foot='',error=''):
    stdscr.erase()
    put(stdscr,1,title,curses.A_BOLD)
    put(stdscr,2,'='*min(56,max(0,stdscr.getmaxyx()[1]-4)))
    for i,line in enumerate(body):put(stdscr,4+i,line)
    y=4+len(body)
    if error:put(stdscr,y+1,'Ошибка: '+error)
    put(stdscr,min(stdscr.getmaxyx()[0]-2,y+3),foot)
    stdscr.refresh()
    return y


def menu(stdscr,title,choices,body=None,timed=False):
    idx=0
    if timed:
        stdscr.timeout(1000)
    try:
        while True:
            content=body() if callable(body) else body
            text=list(content or [])+['']
            text.extend(('❯ ' if i==idx else '  ')+x for i,x in enumerate(choices))
            screen(stdscr,title,text,foot='↑ ↓ — выбор    Enter — подтвердить    Esc — назад')
            try:
                key=stdscr.get_wch()
            except curses.error:
                if timed:
                    continue
                raise
            if key in (curses.KEY_UP,'k'):idx=(idx-1)%len(choices)
            elif key in (curses.KEY_DOWN,'j'):idx=(idx+1)%len(choices)
            elif key in ('\n','\r',curses.KEY_ENTER):return idx
            elif key=='\x1b':return None
    finally:
        if timed:
            stdscr.timeout(-1)


def numeric_input(stdscr,title,lines,validate,initial=''):
    value=str(initial);error=''
    replace_initial=bool(initial)
    while True:
        body=lines(value) if callable(lines) else lines
        visible=max(8,stdscr.getmaxyx()[0]-9)
        if len(body)>visible:
            # Remove decorative empty lines before ever hiding computed values.
            compact=[line for line in body if line]
            if len(compact)<=visible:
                body=compact
            else:
                body=compact[:2]+['… (увеличьте высоту терминала)']+compact[-(visible-3):]
        y=screen(stdscr,title,body,foot='Enter — подтвердить    Esc — назад',error=error)
        # Input line is rendered one line below label.
        put(stdscr,y,value+'█')
        stdscr.refresh()
        ch=stdscr.get_wch()
        if ch=='\x1b':return None
        if ch in ('\n','\r',curses.KEY_ENTER):
            try:return validate(value)
            except ValueError as exc:
                error=str(exc);continue
        if ch in ('\b','\x7f',curses.KEY_BACKSPACE,127):
            value='' if replace_initial else value[:-1]
            replace_initial=False
            continue
        if isinstance(ch,str) and ch in '0123456789-+.,' and len(value)<32:
            value=ch if replace_initial else value+ch
            replace_initial=False
        elif isinstance(ch,str) and ch.isprintable():
            error='Введите только число.'


def wizard(stdscr,env,existing_names=()):
    curses.curs_set(0)
    setup_colors()
    while True:
        idx=menu(stdscr,'OKX GRID BOT — выберите пару',list(ASSETS))
        if idx is None:return None
        coin=list(ASSETS.values())[idx]
        while True:
            try:ctx=load_contract(coin,env)
            except Exception as exc:
                retry=menu(stdscr,f'OKX GRID BOT — {coin}',
                    ['Повторить запрос','Назад к выбору'],
                    ['Не удалось получить спецификацию, BID или VIP комиссии.',
                     str(exc)[:160]])
                if retry==0:continue
                break
            contract_lines=[
                f'Контракт: {ctx["instrument"]}',
                f'Текущая цена:                ${ctx["price"]}', '',
                f'Минимальный ордер:           {ctx["min_qty"]} {coin}',
                f'Стоимость минимального:     ${ctx["min_qty"]*ctx["price"]:.2f}',
                f'Шаг цены:                    ${ctx["tick"]}', '',
                f'Расстояние безубытка:       ${ctx["break_even"]}',
                '(BUY maker + TP maker)',
            ]
            go=menu(stdscr,f'OKX GRID BOT — {coin}',
                ['Продолжить','Назад к выбору'],contract_lines)
            if go!=0:break
            steps=['step','grids','qty','tp','buys','real_tp','upper','name']
            while True:
                j=0
                while j>=0:
                    if j<len(steps):
                        key=steps[j]
                        # Back navigation and explicit edit both preserve typed values.
                        previous=ctx.get(key)
                        initial=(as_string(previous) if previous is not None and key!='name'
                                 else '')
                        if key=='name':
                            val=name_input(stdscr,f'OKX GRID BOT — {coin}',existing_names,
                                           initial=ctx.get('name',''))
                        else:
                            val=numeric_input(stdscr,f'OKX GRID BOT — {coin}',
                                              lambda v,k=key:lines_for(k,ctx,v),
                                              lambda s,k=key:validate_numeric(k,s,ctx),
                                              initial=initial)
                        if val is None:
                            j-=1
                        else:
                            ctx[key]=val
                            j+=1
                        continue
                    # The chosen upper boundary can raise the minimum safe TP.
                    upper=ctx['upper']
                    worst=max(ctx['price'],upper)
                    req_tp=max(ctx['tick'],to_tick_up(
                        fee_break_even(worst,max(Decimal(0),ctx['maker']))
                        *Decimal('1.2'),ctx['tick']))
                    if ctx['tp']<req_tp:
                        screen(stdscr,f'OKX GRID BOT — {coin}',[
                            f'TP ${ctx["tp"]} недостаточен для цены ${worst}.',
                            f'Минимум с запасом: ${req_tp}.',
                            'Нужно вернуться и увеличить TAKE PROFIT.'],
                            foot='Нажмите любую клавишу')
                        stdscr.get_wch()
                        ctx['min_tp']=req_tp
                        j=3
                        continue
                    break
                if j<0:
                    break
                confirm=menu(stdscr,'OKX GRID BOT — СОЗДАНИЕ БОТА',
                    ['Создать бота','Изменить настройки','Отмена'],report(ctx))
                if confirm==0:return ctx
                if confirm==1:
                    # Keep the SAME contract/spec/BID and all chosen values.
                    continue
                return None
            break
    return None



def persist(ctx,out,env_raw):
    out=Path(out).expanduser().resolve()
    if out.exists() and any(out.iterdir()):
        raise RuntimeError(f'Каталог не пуст — ничего не перезаписываем: {out}')
    parent=out.parent
    if not parent.exists():raise RuntimeError(f'Нет родительского каталога: {parent}')
    out.mkdir(mode=0o700,exist_ok=True)
    (out/'state').mkdir(mode=0o700)
    (out/'logs').mkdir(mode=0o700)
    coin=ctx['coin']; ref=ctx['upper'] or ctx['price']
    cfg={'version':VER,'name':ctx.get('name',out.name),'instrument':ctx['instrument'], 'direction':'long',
        'td_mode':'cross','buy_type':'post_only','tp_type':'attached_limit',
        'grid_step':as_string(ctx['step']),'take_profit':as_string(ctx['tp']),
        'order_size':as_string(ctx['qty']/ctx['ct']),
        'buy_base_qty':as_string(ctx['qty']),
        'contract_value':as_string(ctx['ct']),
        'contract_lot':as_string(ctx['lot']),
        'contract_min':as_string(ctx['min_contracts']),
        'fee_group_id':str(ctx['spec'].get('groupId') or ''),
        'tick_size':as_string(ctx['tick']),
        'active_buy_orders':int(ctx.get('buys',25)),'active_tp_orders':int(ctx['real_tp']),
         'real_tp_target':int(ctx['real_tp']),
         'buy_buffer_orders':5,
         'buy_cancel_protect_steps':5,
         'tp_buffer_orders':int(ctx['buys'])+5,
        'max_grids':int(ctx['grids']),'max_exchange_orders':500,
        'upper_price':as_string(ctx['upper']),
        'capital_reference_price':as_string(ref),
        'price_at_setup':as_string(ctx['price']),
        'maker_fee_at_setup':as_string(ctx['maker']),
        'taker_fee_at_setup':as_string(ctx['taker']),
        'partial_buy_timeout_sec':60,
        'partial_buy_distance_steps':5,
        'created_at':time.time(),'live_trading':False}
    cfg_path=out/'config.json'
    fd=os.open(cfg_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as f:json.dump(cfg,f,ensure_ascii=False,indent=2)
    secret_path=out/'.env'
    fd=os.open(secret_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as f:f.write(env_raw)
    shutil.copy2(BASE/'okx_bot.py',out/'okx_bot.py')
    shutil.copy2(BASE/'okx_terminal_accounting.py',out/'okx_terminal_accounting.py')
    shutil.copy2(BASE/'okx_latency.py',out/'okx_latency.py')
    shutil.copy2(BASE/'requirements.txt',out/'requirements.txt')
    return out


def main():
    parser=argparse.ArgumentParser(description='Create a NEW OKX bot directory; no order writes.')
    parser.add_argument('--output',required=True,help='NEW empty bot directory, e.g. /root/okx/btc01')
    parser.add_argument('--env-file',required=True,help='Existing local OKX API .env')
    args=parser.parse_args()
    out=Path(args.output).expanduser().resolve()
    if out.exists() and any(out.iterdir()):
        parser.error(f'Output directory is NOT EMPTY: {out}')
    env,raw=read_env(args.env_file)
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        parser.error('Requires interactive SSH terminal (TTY)')
    result=curses.wrapper(lambda stdscr: wizard(stdscr,env))
    if result is None:
        print('Создание отменено. Торговых операций не было.')
        return
    saved=persist(result,out,raw)
    print('Конфигурация создана:',saved)
    print('Режим live_trading=false; ордера НЕ выставлялись.')
    print('Перед LIVE сначала read-only audit, отдельный systemd и проверка безопасности.')

if __name__=='__main__':
    try:main()
    except KeyboardInterrupt:print('\nОтмена. Ордеров не было.',file=sys.stderr);sys.exit(130)
    except Exception as exc:print('STOP:',str(exc),file=sys.stderr);sys.exit(1)
