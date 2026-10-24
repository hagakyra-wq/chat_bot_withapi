# yubari_banner - генератор баннеров профиля для бота «Юбари»

Зависимости: `pip install pillow numpy`. Шрифты лежат в `fonts/` (DejaVu, кириллица есть).

## Файлы
| файл | что внутри |
|---|---|
| `bn_common.py` | шрифты, `clean_text`, `make_digest` (seed), `palette`, конфиг уровней `LEVELS`, `content_hash` |
| `frames.py` | генератор рамок `draw_frame(rng, level, box, radius, acc)` |
| `banner.py` | движок уровней 1-5 (PNG), `make_banner()` - единая точка входа, `build_parts()` |
| `banner_anim.py` | уровень 6: GIF, 28 пресетов (`PRESETS`), `make_animated_banner()` |

## API
```python
from banner import make_banner
from bn_common import content_hash

buf = make_banner(user_id, name, tag, level=1..6,
                  item_seed=None,   # у каждого предмета инвентаря свой seed (новый выигрыш = новый баннер)
                  avatar=None,      # bytes | PIL.Image | file-like; нужен для уровней 5 и 6, без него рисуется инициал
                  preset=None,      # только level 6: id пресета; None -> выбирается по seed
                  fresh=False,      # True -> новый рандом при каждом вызове
                  salt="yubari")    # смена salt меняет все баннеры разом
# level 1-5 -> BytesIO (buf.name == "banner.png"), level 6 -> BytesIO (buf.name == "banner.gif")
# as_bytes=False возвращает PIL.Image (только level 1-5)

content_hash(name, tag, avatar_file_unique_id)  # для поля content_hash в БД: изменился -> баннер «устарел»
```
Один и тот же (user_id, item_seed, salt) всегда даёт тот же баннер (проверено побайтно).
Информация о пресете: `make_animated_banner(..., return_info=True)` -> `(buf, {"preset", "preset_name", "digest"})`.

## Подключение (python-telegram-bot)
```python
import asyncio
buf = await asyncio.to_thread(make_banner, u.id, u.full_name, u.username, level=6, item_seed=item_id, avatar=photo_bytes)
await (update.message.reply_animation(buf) if level == 6 else update.message.reply_photo(buf))
```
Генерация синхронная и тяжёлая - один воркер/очередь, не больше одной генерации одновременно.
Готовый файл сразу отправить в канал бэкапа и сохранить `file_id`; повторно не генерировать.

## Уровни
| ур. | название | цветов | рамка | прочее |
|---|---|---|---|---|
| 1 | Обычный | 2 | одинарная линия | минимум декора, печать с инициалом |
| 2 | Необычный | 3 | двойная линия | немного боке, кольцо с засечками |
| 3 | Редкий | 4 | линия + угловые вставки | узор на фоне, роза в печати |
| 4 | Эпический | 5-6 | орнамент (зубцы / засечки / волны) | эмблема с розами Гранди и короной |
| 5 | Элитный | 7-8 | двойная + орнамент + камни | аватарка пользователя в круглой рамке |
| 6 | Легендарный | 5-6 | как 5 + огоньки, по контуру бежит свет | GIF 800 px, 30 кадров, 20 к/с, один из 28 пресетов |

## 28 пресетов (id - название)
gears Шестерёнки, starry Звёздное небо, sakura Цветение сакуры, daynight День и ночь, aurora Северное сияние,
synthwave Синтвейв, matrix Цифровой дождь, ocean Закат над океаном, deepsea Глубоководье, snowfall Снегопад,
rain_city Неоновый дождь, storm Гроза, fire Огонь, lava Лава, nebula Туманность, galaxy Галактика,
warp Гипердрайв, fireflies Светлячки, autumn Осенние листья, fireworks Салют, cyber Киберсхемы, radar Радар,
equalizer Эквалайзер, dna Спираль ДНК, mandala Мандала, dunes Барханы, confetti Праздник, hearts Сердечки.

## Как добавить пресет
1. В `banner_anim.py` написать `def p_myname(C): ... return scene(base, layer1, layer2, ...)`.
   `C` содержит `OW, OH` (800×420), `rng`, `acc` (палитра), `bg1/bg2`, `ex/ey/er` (центр эмблемы).
2. Слой = функция `(fr, t)`, где `fr` - PIL RGB кадр, `t` - от 0 до 1 (не включая 1).
3. Правило бесшовности: за цикл целое число оборотов/проходов (`sin(TAU*(k*t+ph))` с целым `k`,
   вращение симметричных фигур на 1/n оборота, частицы либо `life=True`, либо целое число проходов).
4. Добавить строку в `PRESETS`: `("id", "Название", hue_или_None, p_myname)`. Больше ничего менять не нужно.
Проверка петли: `scene_for(user_id, "id")(0.0)` и `(1.0)` должны совпадать.

## Замеры (1 ядро, без GPU)
Уровни 1-5: 1-1.5 с, PNG ~0.5-1 МБ. Уровень 6: 3-4.5 с, GIF 4-6 МБ.
На сервере 0.1 vCPU ждать примерно ×10 (около 30-40 с для GIF), поэтому генерация один раз и хранение file_id.
