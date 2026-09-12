"""V4 admits affirmative, repeated ASMR-like texture evidence only.

AudioSet is not a semantic ASMR oracle. Thresholds express an intentionally
conservative selection policy, not calibrated probabilities of human certainty.
"""
from .common import merge


def positive_asmr(record):
    texture=record.get('texture',0.)
    mouth=record.get('mouth',0.)
    conflict=max(record.get(k,0.) for k in ('speech','expressive','music','airflow','impact','liquid','container','gargle','loud_laugh'))
    return (not record.get('quiet',False) and record['end']-record['start']>=2
            and texture>=.25 and (mouth>=.18 or texture>=.45)
            and conflict<.22 and texture>max(conflict,record.get('breath',0.))*2)


def extraction_regions(records):
    # Two neighboring observations are required; an isolated model hit does
    # not establish a complete intentional ASMR action. Do not bridge unknown
    # windows just because no speech was detected in them.
    rows=sorted(records,key=lambda r:r['start'])
    runs=[]; run=[]
    for r in rows:
        valid=positive_asmr(r)
        if run and (not valid or r['start']-run[-1]['start']>2.1):
            runs.append(run);run=[]
        if valid:run.append(r)
    if run:runs.append(run)
    evidence=[]
    for run in runs:
        if len(run)<2:continue
        # Leave ambiguous context at either end for natural-boundary planning.
        start=run[1]['start'];end=run[-2]['end']
        if end-start>=3:
            evidence.append({'start':start,'end':end,'windows':len(run),
                             'min_texture_score':min(r['texture'] for r in run)})
    return {'intervals':merge([[r['start'],r['end']] for r in evidence]),'evidence':evidence,
            'policy':'Repeated positive texture evidence; unknown audio is not admitted. Scores are not calibrated ASMR probabilities.'}
