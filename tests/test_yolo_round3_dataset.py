import importlib.util
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
try:
 import prepare_yolo_round3 as m
except ImportError:
 m=None
class Round3Tests(unittest.TestCase):
 def test_fixed_first_unused_record_no_class_selection(self):
  self.assertIsNotNone(m)
  names=[f'drafter_{d}/annotations/C{d*12+k}_D1_P1.xml' for d in range(21,31) for k in (2,0,1)]
  used={d*12 for d in range(21,31)}
  chosen=m.select_members(names,used)
  self.assertEqual([m.r2.identity(n)[1] for n in chosen],[d*12+1 for d in range(21,31)])
 def test_no_substitution_for_missing_drafter(self):
  self.assertIsNotNone(m)
  with self.assertRaisesRegex(ValueError,'drafter 21'):
   m.select_members([],set())
if __name__=='__main__': unittest.main()
