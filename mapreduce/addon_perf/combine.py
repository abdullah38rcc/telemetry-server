# Combine output from multiple daily runs of addon_perf.py map/reduce
# into a weekly summary
# usage: combine.py output-path date input-filename [input-filename ...]

import unicodecsv as ucsv
import simplejson as json
import gzip
import sys
from collections import defaultdict, Counter

APP_COLUMN=1
OS_COLUMN=2
VER_COLUMN=3
CHAN_COLUMN=4
TEXT_COLUMN=5

# Accumulate the data into a big dict.
# We use the tuple (app, os, version, channel) as our key
# for each key, keep a Counter dict of text => total count
# e.g. {("Firefox","WINNT","29","nightly"): {"Sessions": 4500, "exception text 1": 5, ...},
#       ("Fennec","Android","30","aurora"): {"Sessions":400, ...}}
exceptions = defaultdict(Counter)

# For add-on performance data, for each key (app info + add-on ID),
# keep a dict of measure name to Counter containing histogram
def dc(): return defaultdict(Counter)
addonPerf = defaultdict(dc)
# measures = ['scan_items', 'scan_MS', 'startup_MS', 'shutdown_MS', 'install_MS', 'uninstall_MS']
measures = ['scan_items', 'scan_MS', 'startup_MS', 'shutdown_MS']
# And keep a separate dict of total session count for (appName, platform)
# across all channels/versions
addonSessions = Counter()

# Keep track of how many different names we see for a given add-on ID
addonNames = defaultdict(Counter)

outpath = sys.argv[1]
outdate = sys.argv[2]

for a in sys.argv[3:]:
    print "processing", a
    f = gzip.open(a, 'rb')

    for line in f:
        try:
            keyblob, datablob = line.split("\t", 1)
            key = json.loads(keyblob)

            if key[0] == "E":
                excKey = tuple(key[1:5])
                exceptions[excKey][key[5]] += int(datablob)
                if key[5] == 'Sessions':
                    addonSessions[(key[1], key[2])] += int(datablob)
                continue
            # otherwise it's an add-on performance data point
            # For now, aggregate over app version and channel
            # so the key is just appName, platform, addon ID
            aoKey = (key[1], key[2], key[5])
            data = json.loads(datablob)
            for measure in measures:
                if measure in data:
                    addonPerf[aoKey][measure].update(data[measure])
            # extract add-on names; might be a single entry, might be a histogram...
            if len(key) == 7:
                addonNames[key[5]][key[6]] += 1
            else:
                addonNames[key[5]].update(data['name'])
        except Exception as e:
            print "Bad line: " + str(e) + ": " + line
            continue
    f.close()

# Write out gathered exceptions data
outfilename = outpath + "/weekly_exceptions_" + outdate + ".csv.gz"
outfile = gzip.open(outfilename, "w")
writer = ucsv.writer(outfile)
writer.writerow(["app_name", "platform", "app_version", "app_update_channel",
                 "message", "count", "sessions"])

print "Generating", outfilename

for key, texts in exceptions.iteritems():
    if 'Sessions' in texts:
        sessions = texts['Sessions']
    else:
        print "No session count for " + str(key)
        sessions = 0

    for text, count in texts.iteritems():
        if text == 'Sessions':
            continue
        line = list(key)
        line.extend([text, count, sessions])
        writer.writerow(line)
outfile.close()

# take a dict of {bucket: count, ...} and return a list of percentiles
# [count, 50th, 75th, 95th]
def getPercentiles(bucketList):
    if bucketList == None:
      return [0, 0, 0, 0 ]

    # Ignore a legacy field in some data
    if 'sum' in bucketList:
        del bucketList['sum']

    points = sum(bucketList.values())
    buckets = sorted(bucketList.keys(), key = int)
    result = [points]
    accum = 0
    b = iter(buckets)
    bucket = 0
    for percentile in [50, 75, 95]:
      while accum < (points * percentile / 100):
        bucket = b.next()
        accum += bucketList[bucket]
      result.append(bucket)
    return result


print "Generating add-on data"

aofilename = outpath + "/weekly_addons_" + outdate + ".csv.gz"
aofile = gzip.open(aofilename, "w")
aoWriter = ucsv.writer(aofile)
aoWriter.writerow(["app_name", "platform", "addon ID", "names",
                   "measure", "Total Sessions", "Sessions with this add-on", "Impact (popularity * median time)", "Median time (ms)", "75% time", "95% time"])

# unpacked add-ons
upfilename = outpath + "/weekly_unpacked_" + outdate + ".csv.gz"
upfile = gzip.open(upfilename, "w")
upWriter = ucsv.writer(upfile)
upWriter.writerow(["app_name", "platform", "addon ID", "names",
                   "Total sessions", "Sessions with this add-on", "Impact (popularity * median time)", "Median file count", "Median time (ms)", "75% time", "95% time"])

# Lump together rare add-ons as 'OTHER' by app/platform
otherPerf = defaultdict(dc)

def writeFiles(key, values, names, sessions, points, median_items):
    times = getPercentiles(values['scan_MS'])
    upLine = list(key)
    upLine.append(names)
    upLine.append(sessions)
    upLine.append(times[0])
    upLine.append(float(points) / sessions * float(times[1]))
    upLine.append(median_items)
    upLine.extend(times[1:])
    upWriter.writerow(upLine)

def writeMeasures(key, values, names, sessions):
    for measure in ['startup_MS', 'shutdown_MS']:
        if not measure in values:
            continue
        hist = values[measure]
        line = list(key)
        line.append(names)
        line.append(measure)
        line.append(sessions)
        times = getPercentiles(hist)
        line.append(times[0])
        line.append(float(times[0]) / sessions * float(times[1]))
        line.extend(times[1:])
        aoWriter.writerow(line)

for key, values in addonPerf.iteritems():
    # Total number of sessions for this app/platform combination
    sessions = addonSessions.get((key[0], key[1]), 0)
    nameSet = addonNames.get(key[2], {})
    if "?" in nameSet:
        del nameSet["?"]
    names = json.dumps(nameSet)
    if 'scan_items' in values:
        try:
            items = getPercentiles(values['scan_items'])
        except Exception as e:
            print "Bad addonPerf: " + str(e) + ": ", key, values
            continue
        # How many data points did we get for this add-on on this app/platform?
        points = items[0]
        # If this addon was seen in fewer than 1 in 100 thousand sessions, ignore
        if (points * 100000) < sessions:
            # keep track of rare add-ons as 'Other'
            for measure, hist in values.iteritems():
                otherPerf[(key[0], key[1])][measure].update(hist)
            continue
        median_items = items[1]
        # Don't bother with packed add-ons
        if int(median_items) >= 2:
            writeFiles(key, values, names, sessions, points, median_items)

    writeMeasures(key, values, names, sessions)

form = "{:>9}{:>9}{:>9}{:>9}"
def dumpHist(hist):
    points = sum(hist.values())
    buckets = sorted(hist.keys(), key = int)
    accum = 0
    print form.format('bucket', 'val', 'accum', '%')
    for bucket in buckets:
        accum += hist[bucket]
        print form.format(bucket, hist[bucket], accum, accum * 100 / points)

# Now write out the accumulated 'OTHER' values
for (app, platform), values in otherPerf.iteritems():
    # for measure, hist in values.iteritems():
    #     print
    #     print app, platform, measure
    #     dumpHist(hist)
    sessions = addonSessions.get((app, platform), 0)
    items = getPercentiles(values['scan_items'])
    key = (app, platform, 'OTHER')
    writeFiles(key, values, 'OTHER', sessions, items[0], items[1])
    writeMeasures(key, values, 'OTHER', sessions)

aofile.close()
upfile.close()
