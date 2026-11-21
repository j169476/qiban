# 像素起居室静图

房间场景使用 **320 × 180** 的画布，左上角为原点。绘制顺序从底到顶依次为当前时段的无人背景、当前季节的窗景、当前姿势的角色。窗景和角色按整张画布在 `(0, 0)` 对齐，透明留白用于定位。所有 WebP 都是单帧无损图片，像素放大使用最近邻采样。

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

家具、桌凳、笔记本电脑、外窗框与床头灯固定在同一块像素底板上。各时段分别处理窗外、室内反光、地板与灯光。

## 当地月份与窗景

沿用北半球的月份划分，以设备本地日期为准。每季一张 **2560 × 180** 的透明横排图，八格从左到右依照上表排列。桌面端只需读取当前一季的横排图，跨时段选取对应的一格。在月初跨季、跨时段同时发生时，窗景、房间光照与人物在同一次黑屏转场中更新。

| 月份 | 季节 | 窗户 | 窗景原稿 |
| --- | --- | --- | --- |
| 3–5 月 | 春 | 开 | `seasons/spring.webp` |
| 6–8 月 | 夏 | 开 | `seasons/summer.webp` |
| 9–11 月 | 秋 | 关 | `seasons/autumn.webp` |
| 12–次年 2 月 | 冬 | 关 | `seasons/winter.webp` |

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
