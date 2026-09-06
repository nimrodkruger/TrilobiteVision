import math
import random

def filtered(g, s, tau, panels=4000):
    # Exact convolution integral, evaluated using composite Simpson quadrature.
    top = min(s / tau, 45.0)
    h = top / panels
    def f(v):
        return math.exp(-v) * g(max(0.0, s - tau*v))
    total = f(0) + f(top)
    for j in range(1, panels):
        total += (4 if j % 2 else 2) * f(j*h)
    return total*h/3

def crossing(g, tau, target, start=0, panels=4000):
    lo, hi = start, max(start*2, tau)
    while filtered(g, hi, tau, panels) < target:
        hi *= 2
    for _ in range(48):
        mid = (lo+hi)/2
        if filtered(g, mid, tau, panels) < target:
            lo = mid
        else:
            hi = mid
    return (lo+hi)/2

rng = random.Random(124)
max_step_error = max_ramp_error = 0.0
for _ in range(1000):
    a = 10**rng.uniform(-3, 0)
    z = 10**rng.uniform(-.3, 2)
    r = math.exp(-a)
    x, count = 1-1/z, 0
    while x > 0:
        count += 1
        x = r*x - 1/z
    nstar = math.log1p(z*math.expm1(a))/a
    assert count == max(0, math.ceil(nstar)-1)
    if z > 1+1/r:
        x1 = 1-1/z
        x2 = r*x1-1/z
        ell, dt = -math.log(x1), -math.log(x2/x1)
        max_step_error = max(max_step_error, abs((1/-math.expm1(-ell))/z-1),
                             abs((1+1/(r-math.exp(-dt))))/z-1)
    C = rng.uniform(.1, .8)
    T = 10**rng.uniform(-1, 1)
    rho = rng.uniform(0, .1)
    N = rng.randrange(2, 20)
    E = math.exp(C)
    times = [T*math.expm1(C)]
    for k in range(1, N):
        times.append(E*(T+times[-1]+rho)-T)
    intervals = [y-x for x,y in zip(times,times[1:])]
    rate = sum(1/x for x in intervals)/(N-1)
    gamma = -math.expm1(-(N-1)*C)/((N-1)*math.expm1(C)**2)
    for recovered in [(intervals[0]-rho*E)/(E*math.expm1(C)), gamma/rate-rho/math.expm1(C)]:
        max_ramp_error = max(max_ramp_error, abs(recovered/T-1))
print('1000 randomized step counts verified')
print('Maximum relative step timing inverse error:', max_step_error)
print('Maximum relative ramp inverse error:', max_ramp_error)
for a,z in [(.01, 10),(.3,10),(.3,26.3)]:
    exact = math.log1p(z*math.expm1(a))/a
    approx = math.log1p(z*(-math.expm1(-a)))/a
    print('COUNT a,z,nstar,S8,N:', a,z,exact,approx,math.ceil(exact)-1)

C, tau, ta = .25, .01, 1.0
for b in [1,100]:
    g = lambda s: math.log1p(b*math.expm1(s/ta))
    ell = crossing(g,tau,C)
    ell2 = crossing(g,tau,C,panels=8000)
    ratio = math.expm1(C)/(b*math.expm1(ell/ta))
    print('EXP b,ideal ell,filtered ell,recovered/true,quadrature delta:',
          b,ta*math.log1p(math.expm1(C)/b),ell,ratio,ell2-ell)

for T in [1., .01]:
    g = lambda s: math.log1p(s/T)
    ell = crossing(g,tau,C)
    rho=.003
    ref=filtered(g,ell+rho,tau)
    second=crossing(g,tau,ref+C,start=ell+rho)
    dt=second-ell
    print('LINEAR T,ell,delay approx,latency weight ratio,ISI weight ratio:',T,ell,
          T*math.expm1(C)+tau,T*math.expm1(C)/(ell-tau),
          T*math.exp(C)*math.expm1(C)/(dt-rho*math.exp(C)))
