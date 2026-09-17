"""
VegTrack — โปรแกรมดู log แบบเรียลไทม์จากบอร์ด B
อ่านข้อมูลที่บอร์ด B ส่งออกทาง USB Serial แล้วแสดงผลสด พร้อมบันทึกลงไฟล์ CSV

วิธีใช้
-------
1. ติดตั้งไลบรารีก่อน (ทำครั้งเดียว):  pip install pyserial
2. เสียบบอร์ด B เข้าคอมด้วยสาย USB
3. ปิด Termite ให้สนิทก่อน (ไม่งั้นจะแย่ง COM port กัน เปิดพร้อมกันไม่ได้)
4. รัน:  python vegtrack_logger.py
5. กด Ctrl+C เพื่อหยุด
"""

import csv
import os
import sys
import time
from datetime import datetime

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    print("ยังไม่ได้ติดตั้ง pyserial")
    print("ให้พิมพ์คำสั่งนี้ก่อน:  pip install pyserial")
    sys.exit(1)

BAUD = 115200
CSV_FILE = "vegtrack_log.csv"


def find_microbit_port():
    """ค้นหา COM port ของ micro:bit อัตโนมัติ"""
    ports = list(serial.tools.list_ports.comports())
    for p in ports:
        desc = (p.description or "").lower()
        hwid = (p.hwid or "").lower()
        if "mbed" in desc or "micro:bit" in desc or "microbit" in desc or "0d28" in hwid:
            return p.device
    return None


def parse_line(line):
    """
    แปลงข้อความจากบอร์ด เช่น
    T=28.7 H=60.3 W=99.2 Days=2.12 Grade=A Impact=0 Event=still
    ให้เป็น dict
    """
    data = {}
    for part in line.split():
        if "=" in part:
            key, _, value = part.partition("=")
            data[key] = value
    return data


def main():
    port = find_microbit_port()
    if port is None:
        print("หา micro:bit ไม่เจออัตโนมัติ")
        print("\nพอร์ตที่พบในเครื่อง:")
        for p in serial.tools.list_ports.comports():
            print("   ", p.device, "-", p.description)
        port = input("\nพิมพ์ชื่อพอร์ตเอง (เช่น COM6): ").strip()

    try:
        ser = serial.Serial(port, BAUD, timeout=2)
    except Exception as err:
        print("เปิดพอร์ตไม่สำเร็จ:", err)
        print("ตรวจสอบว่าปิด Termite แล้วหรือยัง (เปิดพร้อมกันไม่ได้)")
        return

    print("=" * 78)
    print("VegTrack Realtime Logger  |  พอร์ต:", port, "|  บันทึกลง:", CSV_FILE)
    print("กด Ctrl+C เพื่อหยุด")
    print("=" * 78)
    header = f'{"เวลา":<10}{"อุณหภูมิ":>9}{"ความชื้น":>10}{"น้ำหนัก%":>10}{"วันเหลือ":>10}{"เกรด":>6}{"กระแทก":>8}  เหตุการณ์'
    print(header)
    print("-" * 78)

    new_file = not os.path.exists(CSV_FILE)
    csv_handle = open(CSV_FILE, "a", newline="", encoding="utf-8-sig")
    writer = csv.writer(csv_handle)
    if new_file:
        writer.writerow(["timestamp", "temp_c", "humidity_pct",
                         "pct_weight", "days_left", "grade",
                         "impact_total", "event"])
        csv_handle.flush()

    count = 0
    try:
        while True:
            raw = ser.readline()
            if not raw:
                continue
            try:
                line = raw.decode("utf-8", errors="ignore").strip()
            except Exception:
                continue
            if not line or "=" not in line:
                continue

            d = parse_line(line)
            now = datetime.now()
            stamp = now.strftime("%H:%M:%S")

            temp = d.get("T", "-")
            hum = d.get("H", "-")
            wgt = d.get("W", "-")
            days = d.get("Days", "-")
            grade = d.get("Grade", "-")
            impact = d.get("Impact", "-")
            event = d.get("Event", "-")

            # แสดงผลสดบนหน้าจอ
            print(f'{stamp:<10}{temp:>9}{hum:>10}{wgt:>10}{days:>10}'
                  f'{grade:>6}{impact:>8}  {event}')

            # บันทึกลง CSV ทันทีทุกบรรทัด (กันข้อมูลหายถ้าโปรแกรมปิดกะทันหัน)
            writer.writerow([now.strftime("%Y-%m-%d %H:%M:%S"),
                             temp, hum, wgt, days, grade, impact, event])
            csv_handle.flush()

            count += 1
            if grade == "C":
                print("   ** เตือน: ลังนี้ถึงเกรด C แล้ว ต้องรีบขาย **")

    except KeyboardInterrupt:
        print("\n" + "-" * 78)
        print(f"หยุดการบันทึก  |  บันทึกทั้งหมด {count} บรรทัด  |  ไฟล์: {CSV_FILE}")
    finally:
        csv_handle.close()
        ser.close()


if __name__ == "__main__":
    main()