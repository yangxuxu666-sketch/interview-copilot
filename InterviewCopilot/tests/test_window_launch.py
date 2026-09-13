import os
from pathlib import Path
import unittest
from unittest.mock import patch
import main


@unittest.skipUnless(os.name == 'nt', 'Windows app window')
class WindowLaunchTests(unittest.TestCase):
    def test_hidden_python_parent_launches_visible_standalone_window(self):
        with patch.object(Path, 'is_file', return_value=True), patch('main.subprocess.Popen') as popen:
            main.open_window('http://127.0.0.1:8765/launch?token=test-only', Path('data'))
        args, kwargs = popen.call_args
        self.assertIn('--app=http://127.0.0.1:8765/launch?token=test-only', args[0])
        self.assertEqual(kwargs['startupinfo'].wShowWindow, 1)
        self.assertTrue(kwargs['startupinfo'].dwFlags & main.subprocess.STARTF_USESHOWWINDOW)


if __name__ == '__main__':
    unittest.main()
