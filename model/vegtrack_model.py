"""
VegTrack — สมการทำนายอายุการเก็บรักษาผักสด และการให้เกรด A/B/C
ปรับค่าจากข้อมูลการทดลองจริงของทีม (คะน้า 8 มัด, 4 วัน, ชั่งน้ำหนักและให้คะแนนความสดจริง)

หลักการ
-------
1) ผักเสีย (คะแนนความสด <= 2) เมื่อน้ำหนักลดลงเหลือประมาณ 80% ของน้ำหนักเริ่มต้น
2) อัตราการสูญเสียน้ำหนักขึ้นกับอุณหภูมิ (สอบเทียบจาก 2 ระดับ: ห้อง ~30C, ตู้เย็น ~9C)
3) จำนวนการกระแทกทดสอบแล้วไม่พบว่าทำให้เสียเร็วขึ้น (Penalty คำนวณได้ติดลบ)
   จึงไม่ใช้เป็นตัวแปรในสมการ แต่รายงานแยกเป็น "ดัชนีความบอบช้ำ" ให้ผู้ขายดูประกอบ

ไฟล์นี้คือเวอร์ชัน Python สำหรับอ้างอิง/ทดสอบนอกบอร์ด
โค้ดที่รันจริงบน micro:bit (JavaScript) อยู่ที่ /board-a/main.js
"""

# ---------------------------------------------------------------
# ค่าคงที่ที่สอบเทียบจากข้อมูลการทดลองจริง
# ---------------------------------------------------------------
SPOIL_THRESHOLD_PCT = 80.0   # % น้ำหนักคงเหลือ ณ วันที่ผักเสีย

CAL_WARM_TEMP = 29.93        # องศาเซลเซียส
CAL_WARM_RATE = 9.26         # % ต่อวัน
CAL_COLD_TEMP = 8.86
CAL_COLD_RATE = 5.71

SLOPE = (CAL_WARM_RATE - CAL_COLD_RATE) / (CAL_WARM_TEMP - CAL_COLD_TEMP)
INTERCEPT = CAL_WARM_RATE - SLOPE * CAL_WARM_TEMP

MIN_RATE = 2.0
HUMIDITY_MOLD_LIMIT = 95.0

GRADE_A_MIN_DAYS = 1.5
GRADE_B_MIN_DAYS = 0.5

IMPACT_HIGH = 30
IMPACT_MEDIUM = 10


def weight_loss_rate(temp_c):
    """อัตราการสูญเสียน้ำหนัก (% ต่อวัน) ที่อุณหภูมิที่กำหนด"""
    rate = SLOPE * temp_c + INTERCEPT
    return max(rate, MIN_RATE)


def days_remaining(pct_weight_left, temp_c):
    """จำนวนวันที่เหลือก่อนถึงเกณฑ์เสีย (80% ของน้ำหนักเริ่มต้น)"""
    days = (pct_weight_left - SPOIL_THRESHOLD_PCT) / weight_loss_rate(temp_c)
    return max(days, 0.0)


def grade_from_days(days):
    if days >= GRADE_A_MIN_DAYS:
        return "A"
    if days >= GRADE_B_MIN_DAYS:
        return "B"
    return "C"


def damage_level(impact_total):
    """ดัชนีความบอบช้ำ — รายงานแยก ไม่รวมในสมการอายุ"""
    if impact_total >= IMPACT_HIGH:
        return "สูง"
    if impact_total >= IMPACT_MEDIUM:
        return "ปานกลาง"
    return "ต่ำ"


class CrateMonitor:
    """จำลองการทำงานบนบอร์ด micro:bit ในลังผัก 1 ลัง"""

    def __init__(self, crate_id):
        self.crate_id = crate_id
        self.pct_weight = 100.0
        self.impact_total = 0
        self.hours_elapsed = 0.0
        self.mold_warning = False

    def update(self, temp_c, humidity_pct, impacts=0, interval_hours=1.0):
        loss = weight_loss_rate(temp_c) * (interval_hours / 24.0)
        self.pct_weight = max(self.pct_weight - loss, 0.0)
        self.impact_total += impacts
        self.hours_elapsed += interval_hours
        if humidity_pct > HUMIDITY_MOLD_LIMIT:
            self.mold_warning = True
        return self.status(temp_c, humidity_pct)

    def status(self, temp_c, humidity_pct):
        days = days_remaining(self.pct_weight, temp_c)
        return {
            "crate_id": self.crate_id,
            "hours": round(self.hours_elapsed, 1),
            "temp_c": temp_c,
            "humidity_pct": humidity_pct,
            "pct_weight_est": round(self.pct_weight, 1),
            "days_left": round(days, 2),
            "grade": grade_from_days(days),
            "impact_total": self.impact_total,
            "damage": damage_level(self.impact_total),
            "mold_warning": self.mold_warning,
        }


if __name__ == "__main__":
    print(f'rate = {SLOPE:.4f} x T + {INTERCEPT:.4f}  (%/day)')
    print(f'days = (%weight_left - {SPOIL_THRESHOLD_PCT:.0f}) / rate')
    print()
    crate = CrateMonitor("DEMO")
    for t in [30, 30, 30, 25, 25, 10, 10, 10]:
        st = crate.update(t, 60, impacts=0, interval_hours=6.0)
        print(st)
