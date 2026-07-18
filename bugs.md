# BUGS

- [] Some console error: 
2026-06-06 02:11:08,101 - log.py - 141 - ERROR - Uncaught exception in thread Thread-82 (_readerthread)
Traceback (most recent call last):
  File "C:\Python312\Lib\threading.py", line 1075, in _bootstrap_inner
    self.run()
  File "C:\Python312\Lib\threading.py", line 1012, in run
    self._target(*self._args, **self._kwargs)
  File "C:\Python312\Lib\subprocess.py", line 1599, in _readerthread
    buffer.append(fh.read())
                  ^^^^^^^^^
  File "C:\Python312\Lib\encodings\cp1252.py", line 23, in decode
    return codecs.charmap_decode(input,self.errors,decoding_table)[0]
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
UnicodeDecodeError: 'charmap' codec can't decode byte 0x90 in position 32757: character maps to <undefined>

- [] token estimate counter in base.py is a bit off, calculates 53k when LM Studio processes 65k tokens. needs some adjustment 