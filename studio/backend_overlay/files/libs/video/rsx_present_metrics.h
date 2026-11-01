/* RetroRemakes PS3 Studio - backend fixes
 * Copyright (c) 2026 RetroRemakes. All rights reserved.
 * Parts of this file come from open-source projects credited in NOTICE.md
 * (ps3recomp, RPCS3 and others) and remain under their original licenses.
 */
/* Renderer-thread presentation telemetry. Monotonic seconds; no guest changes. */
#ifndef RR_PRESENT_METRICS_H
#define RR_PRESENT_METRICS_H
#include <stdint.h>
#include <string.h>
#include <stdlib.h>
#define RR_FRAME_SAMPLES 256
struct rr_present_metrics {
    double start,previous_present,intervals[RR_FRAME_SAMPLES];
    double batch_seconds,gpu_wait_seconds,present_seconds,ring_wait_seconds;
    uint64_t batches,presents,skipped,failed;
    unsigned sample_count,sample_cursor;int started;
};
struct rr_present_report {
    double seconds,presented_fps,batch_rate,average_ms,p95_ms,worst_ms;
    double batch_ms,gpu_wait_ms,present_ms,ring_wait_ms,host_other_ms;
    uint64_t batches,presents,skipped,failed;unsigned samples;
};
static int rr_metric_double_compare(const void* a,const void* b)
{double x=*(const double*)a,y=*(const double*)b;return x<y?-1:x>y?1:0;}
static void rr_metrics_begin(struct rr_present_metrics* state,double now)
{if(!state->started){state->start=now;state->started=1;}}
static void rr_metrics_batch(struct rr_present_metrics* state,double now,double batch,double gpu,double present,double ring,int attempted,int success)
{
    rr_metrics_begin(state,now);state->batches++;
    state->batch_seconds+=batch;state->gpu_wait_seconds+=gpu;
    state->present_seconds+=present;state->ring_wait_seconds+=ring;
    if(!attempted)state->skipped++;
    else if(!success)state->failed++;
    else {
        state->presents++;
        if(state->previous_present>0 && now>state->previous_present){
            state->intervals[state->sample_cursor]=(now-state->previous_present)*1000.;
            state->sample_cursor=(state->sample_cursor+1)%RR_FRAME_SAMPLES;
            if(state->sample_count<RR_FRAME_SAMPLES)state->sample_count++;
        }
        state->previous_present=now;
    }
}
static int rr_metrics_report(struct rr_present_metrics* state,double now,double minimum_seconds,struct rr_present_report* report)
{
    if(!state->started || now-state->start<minimum_seconds || now<=state->start)return 0;
    memset(report,0,sizeof(*report));report->seconds=now-state->start;
    report->batches=state->batches;report->presents=state->presents;
    report->skipped=state->skipped;report->failed=state->failed;
    report->presented_fps=state->presents/report->seconds;report->batch_rate=state->batches/report->seconds;
    report->samples=state->sample_count;
    if(state->sample_count){
        double values[RR_FRAME_SAMPLES],sum=0;
        memcpy(values,state->intervals,state->sample_count*sizeof(double));
        qsort(values,state->sample_count,sizeof(double),rr_metric_double_compare);
        for(unsigned i=0;i<state->sample_count;i++)sum+=values[i];
        report->average_ms=sum/state->sample_count;report->p95_ms=values[(state->sample_count*95+99)/100-1];
        report->worst_ms=values[state->sample_count-1];
    }
    double divisor=state->batches?state->batches:1;
    report->batch_ms=1000*state->batch_seconds/divisor;report->gpu_wait_ms=1000*state->gpu_wait_seconds/divisor;
    report->present_ms=1000*state->present_seconds/divisor;report->ring_wait_ms=1000*state->ring_wait_seconds/divisor;
    double other=state->batch_seconds-state->gpu_wait_seconds-state->present_seconds-state->ring_wait_seconds;
    report->host_other_ms=other>0?1000*other/divisor:0;
    /* Preserve last successful present across windows, so long stalls stay visible. */
    double previous=state->previous_present;memset(state,0,sizeof(*state));
    state->start=now;state->started=1;state->previous_present=previous;return 1;
}
#endif
