# Hardware & Setup — สรุปสำหรับทำซ้ำโปรเจกต์

เอกสารนี้รวมทุกอย่างที่ต้องมี/ต้องทำ ถ้าจะประกอบ Board A + Board B ใหม่อีกรอบ
(รายละเอียดที่มาของสมการอยู่ที่ `docs/equation.md`, ภาพรวมระบบอยู่ที่ `README.md`)

## รายการอุปกรณ์ (Shopping List)

| อุปกรณ์ | จำนวน | ใช้กับ | หมายเหตุ |
|---|---|---|---|
| BBC micro:bit (v2 แนะนำ — รองรับ ML on-device) | 2 | Board A + Board B | v2 จำเป็นสำหรับ micro:bit CreateAI / Edge AI |
| เซนเซอร์ DHT22 (AM2302) | 1 | Board A เท่านั้น | Board B ไม่ต้องมีเซนเซอร์ |
| สาย Jumper (Female-to-Male) | 3 เส้น | ต่อ DHT22 → Board A | VCC, GND, DATA |
| ตัวต้านทาน pull-up 10kΩ (ถ้าโมดูล DHT22 ไม่มีในตัว) | 1 | Board A | โมดูลสำเร็จรูปส่วนใหญ่มี pull-up ในตัวแล้ว |
| แบตเตอรี่ + battery holder (2xAAA) สำหรับ micro:bit | 1 ชุด | Board A | ต้องไร้สาย เพราะติดไปกับลังผัก |
| สาย USB micro-B | 2 เส้น | Flash โค้ด + Board B ใช้เสียบค้างที่ร้าน | Board B เสียบ USB ตลอดเวลาใช้งาน |
| กล่อง/ลังผัก สำหรับยึด Board A + DHT22 | 1 | Board A | กันน้ำ/กันกระแทกตามความเหมาะสม |

## การต่อวงจร — Board A (Sender)

```
DHT22          micro:bit
-----          ---------
VCC     -->    3V
GND     -->    GND
DATA    -->    P1          (ห้ามใช้ P0 — เคยพบพินเสียตอนทดสอบ)
```

Board B ไม่มีการต่อพ่วงใด ๆ — ใช้จอ LED 5x5 ในตัวแสดงเกรดอย่างเดียว

## ซอฟต์แวร์ที่ต้องติดตั้ง/เตรียม

1. เว็บ [makecode.microbit.org](https://makecode.microbit.org) (ไม่ต้องติดตั้งอะไร ใช้ผ่านเบราว์เซอร์)
2. Extension: **DHT11_DHT22** (`alankrantas/pxt-DHT11_DHT22`) — เพิ่มผ่านเมนู Extensions ในโปรเจกต์ Board A
3. **micro:bit CreateAI** (createai.microbit.org) — สำหรับเทรนโมเดล Edge AI 5 คลาส: `Still`, `SmoothRoad`, `RoughRoad`, `Lift`, `Impact`
   - เก็บข้อมูล motion (accelerometer) แต่ละคลาสจากการจำลองสถานการณ์จริง (วางนิ่ง / วางบนรถวิ่งถนนเรียบ / ถนนขรุขระ / ยกขึ้น-ลง / กระแทก)
   - เทรนแล้ว export กลับเข้า MakeCode project ของ Board A (จะได้ block `ml.onStart(ml.event.X, ...)`)
4. Python 3.x — สำหรับรัน `model/vegtrack_model.py` (ทดสอบสมการนอกบอร์ด ไม่มี dependency ภายนอก)

## ขั้นตอนประกอบใหม่ (ทำตามลำดับ)

1. เปิดโปรเจกต์ใหม่ 2 อัน บน MakeCode — ตั้งชื่อ `vegtrack-board-a` และ `vegtrack-board-b`
2. **Board A**: เพิ่ม extension DHT11_DHT22 → เทรน/นำเข้าโมเดล Edge AI (5 คลาสด้านบน) → สลับแท็บ JavaScript → วางทับด้วยโค้ดจาก `board-a/main.js`
3. **Board B**: สลับแท็บ JavaScript → วางทับด้วยโค้ดจาก `board-b/main.js` (ไม่ต้องเพิ่ม extension ใด ๆ)
4. ต่อ DHT22 เข้า Board A ตามวงจรด้านบน
5. ตรวจว่า `radio.setGroup(11)` ในทั้งสองไฟล์เป็นเลขเดียวกัน — ถ้าจะใช้เลขอื่นต้องแก้ให้ตรงกันทั้งคู่
6. Flash Board A ก่อน แล้วค่อย Flash Board B (ลำดับไม่มีผลจริง แต่ทำตามนี้เพื่อความชัวร์เวลาดีบัก)
7. เสียบ Board A เข้าแบตเตอรี่ ติดเข้าไปในลังผักพร้อม DHT22
8. เสียบ Board B เข้าคอมพิวเตอร์ผ่าน USB ทิ้งไว้ที่ร้าน/โต๊ะสาธิต
9. เปิด Serial monitor (ใน MakeCode หรือโปรแกรมอ่าน serial อื่น) ที่ฝั่ง Board B เพื่อดูข้อมูลที่ส่งมา หรือนำ serial ไปต่อกับสคริปต์อัปขึ้น Google Sheets

## ตรวจสอบว่าประกอบถูกต้อง

- เขย่า/พลิก Board A เบา ๆ → หน้าจอควรขึ้นไอคอน Ghost สั้น ๆ เมื่อ Edge AI จับ event `Impact`
- กด Button A บน Board A → ควรโชว์อุณหภูมิปัจจุบัน (ถ้า DHT22 ต่อถูกต้องจะไม่ใช่ค่า `-999`)
- ฝั่ง Board B ควรเห็นตัวอักษรเกรด (A/B/C) ขึ้นที่จอ LED ทุก ~4 วินาที เมื่อ Board A อยู่ในระยะสัญญาณ radio
- Serial output ที่ Board B ควรมีรูปแบบ: `T=.. H=.. W=.. Days=.. Grade=.. Impact=.. Event=..`

## อ้างอิงเพิ่มเติม

- ที่มาของค่าคงที่ในสมการ (`SLOPE`, `INTERCEPT`, เกณฑ์เกรด): `docs/equation.md`
- โค้ดที่รันจริงบนบอร์ด: `board-a/main.js`, `board-b/main.js`
- เวอร์ชัน Python ของสมการ (ทดสอบนอกบอร์ด): `model/vegtrack_model.py`
