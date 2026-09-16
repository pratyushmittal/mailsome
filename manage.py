#!/usr/bin/env python
import sys

from django.core.management import execute_from_command_line

from mailsome.bootstrap import configure

if __name__ == "__main__":
    configure()
    execute_from_command_line(sys.argv)
