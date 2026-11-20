# 像素起居室静图

八张无人背景和七张透明角色静图共用 **320 × 180** 的画布，左上角为原点。所有 WebP 都是单帧无损图片。背景先画，人物按整张画布在 `(0, 0)` 对齐叠加；人物文件里的透明留白用于定位。需要整数倍放大时使用最近邻采样。

## 当地时间与背景

| 起始时刻 | 时段 | 文件 |
| --- | --- | --- |
| 00:00 | 深夜 | `backgrounds/midnight.webp` |
| 05:00 | 黎明 | `backgrounds/predawn.webp` |
| 07:00 | 清晨 | `backgrounds/morning.webp` |
| 09:00 | 上午 | `backgrounds/forenoon.webp` |
| 12:00 | 正午 | `backgrounds/noon.webp` |
| 14:00 | 午后 | `backgrounds/afternoon.webp` |
| 17:00 | 黄昏 | `backgrounds/dusk.webp` |
| 19:30 | 入夜 | `backgrounds/night.webp` |

家具、桌凳、笔记本电脑、窗框与床头灯固定在同一块像素底板上。各时段分别处理窗外、室内反光、地板与灯光。

## 人物与原稿光照

人物在 `characters/hoodie/`。同名 JSON 键见 [lighting-reference.json](lighting-reference.json)，值为**交付人物颜色所对应的背景时段**，可用作前端调整光照的起点。

| 姿势 | 文件 | 光照基准 |
| --- | --- | --- |
| 桌前写回复 | `replying.webp` | `forenoon` |
| 桌前执笔学习 | `learning.webp` | `forenoon` |
| 懒人沙发看手机 | `watching.webp` | `forenoon` |
| 床上小憩 | `sleeping.webp` | `night` |
| 沙发读书 | `reading.webp` | `night` |
| 站在窗边 | `window.webp` | `forenoon` |
| 镜头前的龙尾 | `tail.webp` | `forenoon` |

写回复与读书直接分离自最早的两张 320 × 180 试稿，人物和手持书沿用试稿像素。写回复保留原日间 11:30 的受光，读书保留入夜灯光。学习、看手机和窗边人物沿用既有静态姿势，配色对齐写回复试稿。睡姿以已有静图为底，保留人物与盖在身上的局部被褶，最终配色对齐入夜试稿。枕头、床架、沙发与桌凳均归背景。
