# Reassembling the bundle

The zip is 147 MiB and had to be split to be transferred. Put all seven
`.001` … `.007` parts in one folder, then rejoin them.

## Windows — no tools needed

```
copy /b cmdm-offline-win_amd64-py311.zip.001+cmdm-offline-win_amd64-py311.zip.002+cmdm-offline-win_amd64-py311.zip.003+cmdm-offline-win_amd64-py311.zip.004+cmdm-offline-win_amd64-py311.zip.005+cmdm-offline-win_amd64-py311.zip.006+cmdm-offline-win_amd64-py311.zip.007 cmdm-offline-win_amd64-py311.zip
```

`/b` is binary mode and is not optional — without it `copy` stops at the first
byte that looks like end-of-file and you get a truncated archive.

Or, if Python is already on the machine (it will be, to run this):

```
python rejoin.py
```

It looks beside itself and then in the current folder, and accepts a folder if
the parts are somewhere else:

```
python rejoin.py C:\Users\you\Downloads
```

Part names do not have to be pristine — a browser that appended `.bin` or a
Windows copy that added ` (1)` is fine, as long as the three-digit number is
still in the name. If it finds nothing it lists what it did see.

That one also checks the result against the recorded SHA-256, which the `copy`
route does not. Verify it yourself with:

```
certutil -hashfile cmdm-offline-win_amd64-py311.zip SHA256
```

and compare against `SHA256.txt`.

## Linux / macOS

```
cat cmdm-offline-win_amd64-py311.zip.0?? > cmdm-offline-win_amd64-py311.zip
sha256sum -c SHA256.txt
```

## Then

Extract it and follow `OFFLINE-INSTALL.md` inside:

```
install.cmd
verify.cmd
```
