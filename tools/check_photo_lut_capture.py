#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["numpy==2.5.4", "Pillow==12.3.0", "rawpy==0.27.1"]
# ///
"""Check one same-exposure Z8 NEF/JPEG pair using the bundled diagnostic looks.

Requires ExifTool on PATH. Inputs are read-only. The NEF's full-size JPEG is
the reference from the same exposure, avoiding noise/motion between shots.
This verifies saved-image behavior and RAW decodability; it cannot establish
sensor-sample identity against an unavailable original or LCD/EVF behavior.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import subprocess

import numpy as np
from PIL import Image
import rawpy


TAGS=('Model','FirmwareVersion','Software','ShutterCount','SubSecDateTimeOriginal',
      'DateTimeOriginal','ExposureTime','FNumber','ISO','ColorSpace','Orientation')


def digest(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source,'sha256').hexdigest()


def metadata(nef,jpeg):
    command=['exiftool','-config','','-q','-json','-n',*('-'+tag for tag in TAGS),str(nef),str(jpeg)]
    rows=json.loads(subprocess.check_output(command,text=True))
    if len(rows)!=2 or any(row.get('Model') not in ('NIKON Z8','NIKON Z 8') for row in rows):
        raise ValueError('Both files must identify a Nikon Z8')
    for key in ('ShutterCount','DateTimeOriginal'):
        if key not in rows[0] or rows[0][key]!=rows[1].get(key):
            raise ValueError('NEF and JPEG are not a confirmed same-exposure pair: '+key)
    for key in ('SubSecDateTimeOriginal','ExposureTime','FNumber','ISO','Orientation'):
        if key in rows[0] and key in rows[1] and rows[0][key]!=rows[1][key]:
            raise ValueError('NEF/JPEG metadata disagrees: '+key)
    return [{key:row[key] for key in TAGS if key in row} for row in rows]


def compare(reference,actual,look):
    if reference.shape!=actual.shape or reference.ndim!=3 or reference.shape[2]!=3:
        raise ValueError('Full-size NEF preview and JPEG dimensions differ')
    hist=np.zeros(256,dtype=np.int64)
    error_sum=identity_sum=diversity_sum=0
    largest_tile=0.0
    for top in range(0,len(actual),128):
        source=reference[top:top+128].astype(np.int16)
        output=actual[top:top+128].astype(np.int16)
        cycle=source[:,:,[1,2,0]]
        expected=cycle if look=='cycle' else source
        delta=np.abs(output-expected)
        error_sum+=int(delta.sum())
        identity_sum+=int(np.abs(output-source).sum())
        diversity_sum+=int(np.abs(cycle-source).sum())
        hist+=np.bincount(delta.ravel(),minlength=256)
        for left in range(0,actual.shape[1],256):
            largest_tile=max(largest_tile,float(delta[:,left:left+256].mean()))
    samples=actual.size
    mean=error_sum/samples
    identity=identity_sum/samples
    diversity=diversity_sum/samples
    matched=mean<8 and largest_tile<12
    if look=='cycle' and diversity<8:
        status='insufficient_color'
    elif look=='cycle' and not (mean+3<identity and mean<identity*.5):
        status='mismatch'
    else:
        status='matched_reference' if matched else 'mismatch'
    return dict(status=status,pixels_compared=int(actual.shape[0]*actual.shape[1]),
        mean_error=mean,p99_error=int(np.searchsorted(np.cumsum(hist),samples*.99)),
        max_error=int(np.flatnonzero(hist)[-1]),max_tile_mean_error=largest_tile,
        identity_mean_error=identity,reference_color_difference=diversity,
        limits=dict(mean_error_below=8,max_tile_mean_error_below=12,
                    cycle_reference_color_difference_at_least=8))


def check(nef,jpeg,look):
    nef=nef.resolve(strict=True);jpeg=jpeg.resolve(strict=True)
    hashes=dict(nef=digest(nef),jpeg=digest(jpeg))
    info=metadata(nef,jpeg)
    embedded=subprocess.check_output(['exiftool','-config','','-b','-JpgFromRaw',str(nef)])
    if not embedded:
        raise ValueError('NEF has no full-size JpgFromRaw reference')
    with Image.open(io.BytesIO(embedded)) as source,Image.open(jpeg) as output:
        source.load();output.load()
        if source.format not in ('JPEG','MPO') or output.format not in ('JPEG','MPO'):
            raise ValueError('Both saved-image inputs must be complete JPEG streams')
        if source.size!=(8256,5504) or output.size!=(8256,5504):
            raise ValueError('Use full-resolution FX 8256x5504 JPEG for this acceptance check')
        result=compare(np.asarray(source.convert('RGB')),np.asarray(output.convert('RGB')),look)
    previews={}
    for jpeg_tag,nef_tag in (('ThumbnailImage','ThumbnailImage'),('PreviewImage','PreviewImage'),('MPImage3','OtherImage')):
        pair=[]
        for path,tag in ((nef,nef_tag),(jpeg,jpeg_tag)):
            content=subprocess.check_output(['exiftool','-config','','-b','-'+tag,str(path)])
            if not content:raise ValueError('Missing '+tag+' in '+path.name)
            with Image.open(io.BytesIO(content)) as image:
                image.load();pair.append(np.asarray(image.convert('RGB')))
        previews[jpeg_tag]=compare(pair[0],pair[1],look)
    statuses={result['status'],*(row['status'] for row in previews.values())}
    status=('mismatch' if 'mismatch' in statuses else
            'insufficient_color' if 'insufficient_color' in statuses else 'matched_reference')
    try:
        with rawpy.imread(str(nef)) as raw:
            samples=np.asarray(raw.raw_image)
            if samples.ndim!=2 or samples.size!=8280*5520 or samples.dtype!=np.uint16:
                raise ValueError('Unexpected full-resolution Z8 RAW sample layout')
            raw_hash=hashlib.sha256(np.ascontiguousarray(samples,dtype='<u2').tobytes()).hexdigest()
            raw_result=dict(decoded=True,shape=list(samples.shape),samples=int(samples.size),
                            decoded_sha256=raw_hash,rawpy_version=rawpy.__version__)
    except rawpy.LibRawError as error:
        raw_result=dict(decoded=False,error=str(error),rawpy_version=rawpy.__version__)
    if hashes!=dict(nef=digest(nef),jpeg=digest(jpeg)):
        raise ValueError('An input file changed during validation')
    return dict(schema=1,status=status,look=look,comparison=result,jpeg_previews=previews,metadata=dict(nef=info[0],jpeg=info[1]),
        input_sha256=hashes,embedded_jpeg_sha256=hashlib.sha256(embedded).hexdigest(),raw=raw_result,
        scope='Saved JPEG versus same-exposure NEF embedded JPEG; RAW decoding only',
        raw_sample_preservation_verified=False,physical_display_verified=False)


def main():
    parser=argparse.ArgumentParser(description=__doc__,epilog=
        'Use a colorful static scene, RAW+JPEG Fine*, full-resolution FX, and '
        'one pair per Off / Cycle / Identity selection. Cycle means the bundled RGB-to-GBR look. '
        'Keep these checks alongside a phone recording of the LCD/EVF and measured capture times.')
    parser.add_argument('--nef',type=Path,required=True)
    parser.add_argument('--jpeg',type=Path,required=True)
    parser.add_argument('--look',choices=('off','cycle','identity'),required=True)
    parser.add_argument('--output',type=Path,help='New JSON evidence file; existing files are never replaced')
    args=parser.parse_args()
    try:
        record=check(args.nef,args.jpeg,args.look)
        encoded=json.dumps(record,indent=2)+'\n'
        if args.output:
            with args.output.open('x') as destination:destination.write(encoded)
        print(encoded,end='')
        status={'matched_reference':0,'mismatch':1,'insufficient_color':2}[record['status']]
        return 2 if status==0 and not record['raw']['decoded'] else status
    except (OSError,ValueError,subprocess.CalledProcessError,rawpy.LibRawError) as error:
        parser.exit(2,'ERROR: '+str(error)+'\n')


if __name__=='__main__':
    raise SystemExit(main())
