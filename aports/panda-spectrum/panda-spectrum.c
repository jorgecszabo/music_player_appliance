/*
 * panda-spectrum: the audio spectrum for the front panel (panda-lcd).
 *
 * Records the output's monitor through pw-cat (S16 stereo at the graph's rate,
 * so PipeWire doesn't resample), downmixes to mono, halves the rate (averaging
 * pairs), and FPS times a second runs a 256-point fixed-point FFT on the
 * newest samples: kissfft built with FIXED_POINT=16, a Hann window from an
 * int16 table. Writes BANDS bytes per frame to stdout: log-spaced bands from
 * 60 Hz up, 0-255 on a log (dB) scale, with a falling peak.
 *
 * Integer only in the per-frame path: the ARM1176 (Raspberry Pi 1) has VFPv2
 * and no NEON. Floating point is used once, at start, for the window and the
 * band edges (and inside kiss_fft_alloc for the twiddles). The package's
 * check() fails if objdump finds float instructions in the FFT or in
 * analyze().
 *
 *   panda-spectrum [rate [target]]   record target's monitor (default 48000 panda-eq)
 *   panda-spectrum --stdin rate      raw S16LE stereo from stdin instead (tests)
 *   panda-spectrum --selftest        synthetic tones through the whole path
 */
#include <errno.h>
#include <math.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/wait.h>
#include <unistd.h>

#include "kiss_fft.h"

#define N      256      /* FFT points */
#define BANDS  24
#define FPS    8
#define F_LO   60.0     /* Hz, the lowest band's lower edge */
#define FALL   24       /* how far a band's peak falls per frame (of 255) */

static int16_t hann[N];
static int edge[BANDS + 1];         /* FFT bins: band b is [edge[b], edge[b+1]) */
static kiss_fft_cfg cfg;
static uint8_t level[BANDS];

static void setup(int rate)
{
	double fs = rate / 2.0;          /* after halving */
	double f_hi = fs / 2 < 16000 ? fs / 2 : 16000;
	for (int i = 0; i < N; i++)
		hann[i] = (int16_t)lrint(32767.0 * 0.5 * (1.0 - cos(2 * M_PI * i / (N - 1))));
	int prev = 0;
	for (int b = 0; b <= BANDS; b++) {
		double f = F_LO * pow(f_hi / F_LO, (double)b / BANDS);
		int bin = (int)lrint(f * N / fs);
		if (bin <= prev)
			bin = prev + 1;      /* at least one bin per band */
		if (bin > N / 2)
			bin = N / 2;
		edge[b] = bin;
		prev = bin;
	}
	edge[0] = edge[0] < 1 ? 1 : edge[0];        /* never the DC bin */
	cfg = kiss_fft_alloc(N, 0, NULL, NULL);
	if (!cfg) {
		fprintf(stderr, "panda-spectrum: kiss_fft_alloc failed\n");
		exit(1);
	}
}

/* 8 * log2(p), integer part plus 3 fraction bits. */
static unsigned ilog2_q3(uint32_t p)
{
	if (!p)
		return 0;
	unsigned n = 31 - (unsigned)__builtin_clz(p);
	unsigned frac = n >= 3 ? (p >> (n - 3)) & 7 : (p << (3 - n)) & 7;
	return n * 8 + frac;
}

/* One frame: N mono samples (oldest first) -> level[]. Integer only. */
__attribute__((noinline)) void analyze(const int16_t *x)
{
	kiss_fft_cpx in[N], out[N];
	for (int i = 0; i < N; i++) {
		in[i].r = (int16_t)(((int32_t)x[i] * hann[i]) >> 15);
		in[i].i = 0;
	}
	kiss_fft(cfg, in, out);          /* fixed point: scaled by 1/N */
	for (int b = 0; b < BANDS; b++) {
		uint32_t p = 0;
		for (int k = edge[b]; k < edge[b + 1] && k <= N / 2; k++) {
			uint32_t q = (uint32_t)((int32_t)out[k].r * out[k].r) + (uint32_t)((int32_t)out[k].i * out[k].i);
			if (q > p)
				p = q;
		}
		/* 8*log2(power) = 16*log2(magnitude); 16 per 6 dB. Magnitude 4 (about
		 * -78 dB of full scale after the 1/N) is the floor, 16384 the top. */
		int v = ((int)ilog2_q3(p) - 32) * 255 / 192;
		v = v < 0 ? 0 : v > 255 ? 255 : v;
		int fallen = (int)level[b] - FALL;
		level[b] = (uint8_t)(v > fallen ? v : (fallen > 0 ? fallen : 0));
	}
}

/* Sample path: stereo S16 frames in, a frame of bands out every hop samples. */
struct path {
	int16_t ring[N];
	unsigned pos, hop, count;
	int have_half, half;
};

static void path_init(struct path *s, int rate)
{
	memset(s, 0, sizeof(*s));
	s->hop = (unsigned)(rate / 2 / FPS);
}

/* Returns 1 when level[] has a new frame. */
static int path_push(struct path *s, int16_t l, int16_t r)
{
	int m = ((int)l + r) / 2;
	if (!s->have_half) {
		s->half = m;
		s->have_half = 1;
		return 0;
	}
	s->have_half = 0;
	s->ring[s->pos++ % N] = (int16_t)((s->half + m) / 2);
	if (++s->count < s->hop || s->pos < N)
		return 0;
	s->count = 0;
	int16_t x[N];
	for (int i = 0; i < N; i++)
		x[i] = s->ring[(s->pos + i) % N];
	analyze(x);
	return 1;
}

static int write_all(int fd, const void *buf, size_t n)
{
	const uint8_t *p = buf;
	while (n) {
		ssize_t w = write(fd, p, n);
		if (w < 0 && errno == EINTR)
			continue;
		if (w <= 0)
			return -1;
		p += w;
		n -= (size_t)w;
	}
	return 0;
}

static int run(int fd, int rate)
{
	struct path s;
	int16_t buf[2048];
	path_init(&s, rate);
	size_t have = 0;                 /* bytes in buf (a frame may straddle reads) */
	for (;;) {
		ssize_t n = read(fd, (uint8_t *)buf + have, sizeof(buf) - have);
		if (n < 0 && errno == EINTR)
			continue;
		if (n <= 0)
			return n < 0 ? 1 : 0;
		have += (size_t)n;
		size_t frames = have / 4;
		for (size_t i = 0; i < frames; i++)
			if (path_push(&s, buf[2 * i], buf[2 * i + 1]) && write_all(1, level, BANDS))
				return 0;            /* the panel went away */
		size_t used = frames * 4;
		memmove(buf, (uint8_t *)buf + used, have - used);
		have -= used;
	}
}

static pid_t child;

static void on_term(int sig)
{
	(void)sig;
	if (child > 0)
		kill(child, SIGTERM);
	_exit(0);
}

static int record(int rate, const char *target)
{
	int p[2];
	char rate_s[16];
	snprintf(rate_s, sizeof(rate_s), "%d", rate);
	if (pipe(p))
		return 1;
	child = fork();
	if (child < 0)
		return 1;
	if (child == 0) {
		prctl(PR_SET_PDEATHSIG, SIGTERM);
		dup2(p[1], 1);
		close(p[0]);
		close(p[1]);
		execlp("pw-cat", "pw-cat", "--record", "--target", target,
		       "-P", "{ stream.capture.sink = true, node.name = panda-spectrum, "
		             "node.description = \"Front panel spectrum\", node.passive = true }",
		       "--format", "s16", "--channels", "2", "--rate", rate_s, "-", (char *)NULL);
		perror("panda-spectrum: pw-cat");
		_exit(127);
	}
	close(p[1]);
	signal(SIGTERM, on_term);
	signal(SIGINT, on_term);
	int rc = run(p[0], rate);
	kill(child, SIGTERM);
	waitpid(child, NULL, 0);
	return rc;
}

/* Tones through the whole path; the loudest band must be the tone's. */
static int selftest(void)
{
	const int rate = 48000;
	const double tones[] = { 100, 440, 1000, 3000, 8000 };
	int fails = 0;
	setup(rate);
	for (size_t t = 0; t < sizeof(tones) / sizeof(tones[0]); t++) {
		struct path s;
		path_init(&s, rate);
		memset(level, 0, sizeof(level));
		int frames = 0;
		for (int i = 0; i < rate && frames < 4; i++) {
			int16_t v = (int16_t)lrint(8000 * sin(2 * M_PI * tones[t] * i / rate));
			frames += path_push(&s, v, v);
		}
		int best = 0;
		for (int b = 1; b < BANDS; b++)
			if (level[b] > level[best])
				best = b;
		double fs = rate / 2.0;
		double lo = edge[best] * fs / N, hi = edge[best + 1] * fs / N;
		/* A bin's energy leaks into its neighbours: allow the band next door. */
		double lo2 = edge[best ? best - 1 : 0] * fs / N, hi2 = edge[best + 2 <= BANDS ? best + 2 : BANDS] * fs / N;
		int ok = frames > 0 && tones[t] >= lo2 && tones[t] < hi2 && level[best] > 100;
		printf("%6.0f Hz: band %2d (%5.0f-%5.0f Hz) level %3d %s\n", tones[t], best, lo, hi,
		       level[best], ok ? "ok" : "FAIL");
		fails += !ok;
	}
	/* Silence: everything at 0 once the peaks have fallen. */
	struct path s;
	path_init(&s, rate);
	for (int i = 0; i < rate * 2; i++)
		path_push(&s, 0, 0);
	int sum = 0;
	for (int b = 0; b < BANDS; b++)
		sum += level[b];
	printf("silence: sum %d %s\n", sum, sum == 0 ? "ok" : "FAIL");
	fails += sum != 0;
	return fails ? 1 : 0;
}

int main(int argc, char **argv)
{
	if (argc == 2 && !strcmp(argv[1], "--selftest"))
		return selftest();
	if (argc == 3 && !strcmp(argv[1], "--stdin")) {
		int rate = atoi(argv[2]);
		if (rate < 8000 || rate > 384000)
			return 2;
		setup(rate);
		return run(0, rate);
	}
	int rate = argc > 1 ? atoi(argv[1]) : 48000;
	const char *target = argc > 2 ? argv[2] : "panda-eq";
	if (rate < 8000 || rate > 384000 || target[0] == '-') {
		fprintf(stderr, "usage: panda-spectrum [rate [target]] | --stdin rate | --selftest\n");
		return 2;
	}
	setup(rate);
	return record(rate, target);
}
