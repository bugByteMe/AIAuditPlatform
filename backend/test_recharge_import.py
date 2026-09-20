from __future__ import annotations

import sys
import unittest
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parent))

from openpyxl import Workbook

from recharge_import import parse_recharge_workbook


def workbook_bytes(*, status: str = "支付成功", amount: str = "50.00", row_item: str = "¥50.00 -") -> bytes:
  workbook = Workbook()
  target = workbook.active
  target.title = "Target"
  target.append(["总收款笔数：1"])
  target.append(["总收款金额：50.00"])
  target.append(["收款项：¥50.00 -"])
  for _ in range(5):
    target.append([""])
  target.append(["支付时间", "支付金额", "付款人昵称", "收款项", "支付单号", "订单状态", "付款备注", "用户名"])
  target.append(["2026-09-20 14:39:07", amount, "-", row_item, "2026092022000000000000000001", status, "-", "alice"])
  ignored = workbook.create_sheet("Ignored")
  ignored.append(["收款项：由付款方填写金额"])
  output = BytesIO()
  workbook.save(output)
  workbook.close()

  # Reproduce payment exports whose worksheet dimension incorrectly says A1.
  source = ZipFile(BytesIO(output.getvalue()))
  rewritten = BytesIO()
  with source, ZipFile(rewritten, "w", ZIP_DEFLATED) as target_zip:
    for entry in source.infolist():
      content = source.read(entry.filename)
      if entry.filename == "xl/worksheets/sheet1.xml":
        content = content.replace(b'<dimension ref="A1:H10"', b'<dimension ref="A1"')
      target_zip.writestr(entry, content)
  return rewritten.getvalue()


class RechargeImportTest(unittest.TestCase):
  def test_reads_target_sheet_despite_incorrect_dimension(self) -> None:
    result = parse_recharge_workbook(workbook_bytes())
    self.assertEqual(result["targetSheetCount"], 1)
    self.assertEqual(result["ignoredSheetCount"], 1)
    self.assertEqual(result["records"][0]["status"], "candidate")
    self.assertEqual(result["records"][0]["amountCny"], "50.00")
    self.assertEqual(result["records"][0]["creditCny"], "40.00")

  def test_rejects_non_success_and_mismatched_amount(self) -> None:
    failed = parse_recharge_workbook(workbook_bytes(status="已退款"))["records"][0]
    mismatch = parse_recharge_workbook(workbook_bytes(amount="49.00"))["records"][0]
    self.assertEqual(failed["status"], "invalid")
    self.assertEqual(mismatch["status"], "invalid")

  def test_rejects_non_xlsx_content(self) -> None:
    with self.assertRaisesRegex(ValueError, "valid XLSX"):
      parse_recharge_workbook(b"not-a-workbook")


if __name__ == "__main__":
  unittest.main()
