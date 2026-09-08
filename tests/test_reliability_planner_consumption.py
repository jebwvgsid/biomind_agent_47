"""Recorded Planner READ-correction L1 acceptance at production contract seams.

The compressed fixture is an exact copy of prior read-only audit recordings,
not a newly generated correct Plan. It contains both original provider bodies,
ControllerBinding, TaskSpec, and first complete feedback from session_1788877967.
No original session, approved script, input, or external service is accessed.
This file tests deterministic admission/compilation; a real Planner-loop test
must independently prove feedback visibility and human semantic review.
"""
from __future__ import annotations

import base64
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import zlib

import pytest

import agent_core.node_context as node_context
from agent_core.node_context import ControllerBinding, resolve_plan_proposal_handles
from agent_core.plan_bindings import compile_plan_resource_bindings
from agent_core.plan_contract import (
    parse_planner_outcome_envelope,
    prepare_allocation_intent,
    validate_planner_candidate_deterministically,
    validate_planner_consumption_correction,
)
from agent_core.plan_tracker import build_plan_state, normalize_plan_contracts
from agent_core.skill_manager import SkillManager
from agent_core.step_effects import compile_plan_dataflow, compile_plan_step_effects
from agent_core.task_spec import effective_output_contracts


# zlib + base85 keep the two complete 38k-character recordings in this one
# standalone file. The original public-response digests are checked below.
_RECORDED_B85 = (
    'c-rlK>ysPDk>|fsF?SJXZvYse2lId^D|Sc@Wu7?{IYUVwBu1+rg_#xsG}zsk;mBl!_ipcG@7k8{)~~Z2wsp488yjKC7rTB*vi--L'
    'IHW%9zi^pZRsC)N4PdAlus=vcbXQkbS7!b)>-Ed8u2{yvc5EZyE!{JYS6*N%Z@u-_+rB$^bMVzS2aLVB;*J8#?Q=ai8S*!wz?&=k'
    'uU_80en-D`WBaCl?Ug-!Z|Ak^JFmaFQWFmhJ$LB(+|~^*aJq&S`1sfxc$2ZOia%Jc=UPTj_jp&-I_w#P2K?e4A6Q&>1~z}deOaq`'
    '!smTs5IB~8<nUvBDy$9fdTviPFjb3gjs`Z~tHq0VU9WEh;sNv6(0!vHxNgtahmK`<c1{EEDVzJ2=L`b}dWasq{Q5SdqBW*!U|qwp'
    ';Wu<GyG4~TpWh#0gP84D_^jcbFlYcf-~;H}M%VBI<INTIqDQfBR-LF@?jZ2sADQoV&9^z+zXz=lO|l&iF1X%Fs_i|e%dHbj^%8ct'
    'PC`hP^SJMhJm|fsN~hl+1%}z<p-@xLKv#0lF?!BB27aGDbbF3<qC4_=kehM9u~VT~pW6-;*N<AUPZs}BUp_%i{FdG~ynFU&sGCNA'
    'CLMuip@rBCtO=UtyIuf)lJ#X2C382U5LaB+vz-CdTo+Zc@Kts3Dt38$m!uC0GwgA;-&&aHyG};WmsRS*KxdMD-qTr0JCh`R9jhQg'
    '?zeJ<T#0o`v37gL0U%&7lQf~72ZKHz1p0j|ucowQg9pJR$wo*nt7G5=hoe9WyOQgaa<$we?m?aN%QdH_dE~09tK>S9ToHo%Jn$UL'
    'pV#?jYMDW-GRWEm;JvD9_L;1@l5H8;YD*PZX_eH=OZ{90R$3KU%_G)}6<Af~I;~tUR$x`hby~SzyuhlG?NqWw&)9SPU}m?PdN?CY'
    '|7emh_bDi<2W}n1q@fWUGU_0bqwO9K{J`TzpS|tEH#7Kzo-;V8q&wx1=-|le&LmtBtEQh%%NK9!2k%Uh@t2KZ&&8SgD<D4Zv6o-F'
    'a;=(boLQiAgu=OxXROwo7z^JqOy_}EVNg?zDAY=fQ+m`3VwaU0EhEtLXqQ@Lms5+;a{Hz;fcj>(&aei{J#@WbL1ubI)QW=iRUAb1'
    'M+OV{i|e|T!OkG*Im~u^Jqxg3^@6y-D-ln%XTvzwXEkGdmh4O)Z@<%-@5_8ZPFC12)GcjT*{%?6CDjsAox@~{YFL1=CPZ7wb6R=&'
    'gW+73ntgsYJKeu|tL_eZC(Iw2J`b4Ntt2{^MCY{9qFNSYsUYr3s%4~#-PfDTQuAt<%~r1uuJT@Gt&5fGoc3B=&w?zLgkH&ZTG_(K'
    'K|Q$DVGaj7rjpq#_sfQ7*Hx_wgxRqZ9O4gM+=0O?#LU!5ekJT#ggu8XPpoPI=A2f5O30;zJfABbTg@V@Is|HA*Vk`e-MLX&_QHg|'
    '2-`kWXPs4qO4QScdT99mT;95%g4t~P*7p9s2(guyHo9oR`@9D-dCh=+83aD7*ZBjhH?nzX|CO-I3VRNV9$U=<Y#Ftn^3tURJD-Ox'
    'tYThEjy!W^%V#F?d9C?OeKXl}<(JE??*qKzw&2-zKblro9O0!Z5Uc0g{;29>R9^+sGl^8#*usQL)t!x4D_bok*dyV~pgTr`8D(2o'
    '#SGh9rE6tW7xPE`J|42C{(9c!iGS`&b_PG-!vaDEycJeH1Y!4_gTnx-AC6M;4V*xI@GVu2Q8{)5E$-q*acD5@ltEKt#*yLlz(4k!'
    ';DlL++`5;j&#)pn@(0`+1<8{Q!;y!S5=v=$$XG!S#M=b#QQM{c!^AC7CHA0Y3}q|A4<!l)$MPG;2Y$oH4FV018+M~_42RC(0PxEn'
    'ax1xU+Z}Sxh?<;NMD<$O(Xq=hnhVMNP|KDX8V*h$V1fIM^=4<wHrLu#bE9KgCZGI7%j8<eGPJhY(u@tWGx>?GwP`dr*SZ@Urna%w'
    'nx<8nW;Q!(oNH~~-t1VB=G~2V7M%nUT@k-N^jv#nabGvVBA}<olyFU-dq-+6E7hfuKE%uAx8>ON?)gIlIv!0gs_$+>TkxcV6X?EU'
    'bDej=zH?e{^C1UUG_V|=z3GoEXtu8dLXib$+PvNCv95E(rO_a+_>4br{D5r6@HiW|gF5dIgEWiBwe9^ox8)O9jy|6qyyFb%Q3Nx?'
    '889IzhN_$f);*<t4C~rvjvu{@>cEEtZYG{HkBwFMuioc<En+*Vu>&KxS7XYn)|enrp~gwO?{S8ET?2qC-2`;JM=i5l&xU`{%q4#S'
    '7{L0D?}H{VVas^}^zS*n9@uVH4U`?Pf*se%j_ddxb!o@N`q1sU$GT{nsyAN5Gy>}|M2m!tdyaok@9`rpfd?v5ugUSDX?Pt%2kGMp'
    'Z(=xmdF1qLcKP+~c+bZeX2Ob?tSE<HW>&-nKhrz$9sil&ZKh|&A@BIY<uG^>cEzhGA}h+PV;-Da9CpQ9Z$XC#xw@Cv{tVJ3IwBJ$'
    '<XY_{JdEop{NJf5%|*T>q30pdlEl6*7=3fq7z_ZCf>r32Rp0m`VUGAZI|W|XV~-}_RU)3Up5Ty6Fp9TH;Pi4s60}N}haUV^5e)4b'
    'L)-!_y^HWg1mGZ^`tHsypc8y%jp14C=;*eDD(DZiUe0b`xgcPa*P09jk@4!JTGPYNvk33ThnZzCY%C5IvT?B~nVZ;nDj+tVQHB)}'
    'lbl~LE(5PcVZux}yWeV{eKw>t@K`EYCj7%8Sd?F%Mo&C3dI*NWJr!X3>~Hs8mb*!IzqH><JrEuUzdv%Udo<!>IOPiH3*$mS5Mpsi'
    'j`yYHYvCbsff^x-L<f(vD1HH<8At4+{w(OMjYVgzLg&Sz^CAYupCdYJ6VX|l5}md2=&X%LXRSi#N$9Md5jtz-&{?a{xg<Kv5mheB'
    'Lkyp<P%(Mvw=@&ObBoE-Mwuz3D-D&A_?7^iiiUG68%)duR?t}u>@0R?|FsBq!sD^bbB}$2K$hzvAu&_Ro}Q1q26~X4ganJI+q~!W'
    'ogltBVi`x?XyM{uYw^H)Z!};pZ{PU}%cT<NyEaere4IbHz=>j9;umYeXBfUs5n9bUXq{zroO3umk7Z=}Kxfu5>Olj#rE&0%h<*Zd'
    'Z1h70kbw^oiRG4+HZm~!5VW8{b3Z|hGXun+X4gTp>vDU-xr<LCgwYm+Fj|E$6A)&`f!8b}yh4Q0mKkBR*~eL*1Hx#_jWB{ggVe2X'
    '5mWN3#G*t&)j(k)VNaZar2?3VA<P83p8-sYsCn2}MwcZ3mVzLhWxVH1kRzx^!p<v)KUWktO&K79hx&MTgtIyxd^s+PvU*+axr6r|'
    'jF1FYS|8Zg$moeT&>*b_Z-G(@%K&$m8ov%CcZ{H;nHVD_sb(-PO3+2g+Ro{;&3OW`6yB8Igppw%Vg(d7%YacyCp!-kLFbf#pgrT5'
    'l>i?s8qNusE>1&-E?)qOztm{rgQx>KESBD{o`92mES4gwg1;`EFu?8-ThaybMm$VPo+hd=X2LR>!ibsBCI6J?L<$}@iP43Pr3P67'
    'v`V|wncxa>3e_lvRd`8gmApuHK?tVyK8`^$b;LG=r|{sd*pcdAik;m{782KGvGf`of?Uz|vJfPf81fD=X`(pHm^yitfV}PD5PFh8'
    'Ov2YSct+p=NdfDT=h`b^n*!KEcq$EU6BuX4vYjjeJY{_JO!%ZaC=F7R0;+3*qyxUggZfeVA+)-EmA%NcMw2~!$aZhTzuQgm<JvxZ'
    'iEYRq`>*W5uQgR{&lud>AN8Shy`?qQ@!}pg>|3rMyR+43<~M-msNmHRCD3z&Qe#82u{b3hL4{Q1kSZ1g&5l)LBdKE2l?t!Yc$J8t'
    '7J^s=Qd%>CQmy7<VJgmWwA>>|8sX`Rbfj<uNyS)_LFP!YOjHBL+0ZH*8I=aCF$6({q%=zaQkhY}Odyp*h%^O2;h{=7ID-+FIMa{M'
    '9vVlSUAunc&d%PK@Zwb4kDiAiTO4D?&r_vQMg%|=&MXRN<_V3)QW%XP%~>+EnI|a9rERJZXYq(5&+2m>J~)RHCI&gL$&z8G`pH8o'
    'DH0dFcNAv;@y{p0#CbxWsX~-_kSUMNiH+w4VnysS%P@c|4Cz_EGoI}zHE0-9))Vs0MW;JxH7=yFiWZqtJ8oDGQBuzAHm-b&^}R`z'
    'B?a0D{3G0z;Cg|`)IwAS*eY>f<XVc?xzxCLjMCIae-t6&>sUtiS)*?=+qGx|2NGtew%0(=9t@RnN~$JLw)>;ra~p}B!Kvc2J{MPe'
    '<}g}>;o^EjY2^|ia9;Q^+p1(}g%HM}@dkXr0+-k$Waewz!9y>ucamY>!AEf#tw$@1$A>2@|C9>ZZx+26VIDjP{+$W0k=?E#Ug8Xh'
    'W7KD=#@knFh_<VQw8?m>Ca1Dv6MPavjKK+0tUZK{ejaB}*zqS945>>T)Cpl);*g-An+VvVbi+Sn0ExlKBZ?!?l<tVe58}Rq2LLU('
    'X>aHZdCwW}=NYyOvR+TU^|e-hZ|j)NWM>&HM?jm>s|s=xkQ>=iY1G}P9*y>M7+`tQ*dW&Q+<uxsOW3IAjt+2J2N)B}^Rcp82g%#e'
    '4CoYZxa&6J5)w3Tw6B(KXIs16kSX4ZJvj2m!)3gr93<fU3W|dp)}gpB=1X9d03sarIIYRzKm?r>%X!Q57x0!mDt!?VcAh|a26RrU'
    'aD~jVnTxc^mPY5D$g<Jn`_hKl7o>ed;Hk`;+g}_XeL_GD@0Dc>CSDU~%SCmPKf{ZV?E+$Poai@3i%J9^B9zMG{iWN{K`#7N7w`g|'
    'Wc3U$(T_RQ%ORWT9mBu3KjfD1cOXA6MKy}J-E~JEi?NtW);`ekQ6HpXo-pDlrm_Q<jRvX_(u+EN<g4v5ZYb}wq>a~e=JRvDD=`4B'
    'v4SwI7W3!{ln@S!)&fXbdne2tglh>NedZ<gp!lPMg^+?C;)xrgch1-j=%^c%{O*BUcYAhyXm~*j#+W0YkN@(UF11e3$D-|pJ?_(R'
    '@D^6Y-f@PCsPXFM8WtOR4i&~ZC#G1fFHy7=XQ@tZ(1rMmLrh0e7=CytZ=obfe#Rut<dmN;hriGbK^+WvIGiN;kW5@~uSks)AcK>r'
    'YCKKXFQJ-JcVTfqbADP#j-)R?DZwT=y$A_h)TC^^?>tDKFUn@b#x+^cClI(TQecY@s>uTf(K#id#bOEoeqM?eSk&XRKGqdHzF^yO'
    'C5A$;feewuwVIOOrpVE8`olVuG1T}k+d1N33gp@UCZSsiBr$|s;1fb+gR60ClyB_uxC_CDN`8WjeCT9OedH%6`*PYJKj<nNxwwOR'
    'H8w<eL65}_M&JdA7`N2t2t}yJ=7Pq54o=l)=8wX&&$q9>s5P1oAMW0M5qGpce0XjDr49PW{wsSgt%+N58`z7qJ?!Db<mRv!ach{`'
    'nJ-_It{p0+-DjfKR&&~FOLmP6E$YH8Of<pkT|!Kv!3RB*0ZWaLj+k-$p-bVS41ll<gyyKJa?Z^eWJg5;6xAdoDMmEf%mzgSQS$s&'
    '*DyU|?3&Ga)>Byt#(#a-##&+`OrTBnt2{a{FRQ0d$M`lkgcKj`8(p;F9yA5qEXEVKQ_#<ii26J*AR;nic^)9HgKz5_I-rR!8?DvU'
    'Jwz7(xkQ2p40pW|*dT7(pllCE5fer%&4YR-Q8Si$BwG_etfWh?pX$<r{0y>in1v%XKOY}w04}8Ze~he)>n)z<OuQi!Yx$QHP7FjZ'
    'NNY(_wr>vL?^|!ZwNhg%7AO@BX*3<inegxm+<4oE&L4bLbUM5hfST**oh5L+dU^Ny9sSmg?VI|wSN8P1o!74Kye^wUBQ~5Fu)`D`'
    '1+vLa`8<}}ed>hI$y#(10^$rce>_);d2Hy4;7=D^{5cKCwbqbhJ0h6}xNz(T395OY-ycydhUDDvEaq9slFctD<x0_zafxzX70uR6'
    'uOlG|qb^*Bjx>_+Cp)*3P?XHwoG69n-k|2Xs50VvtV=J2tZpghu_}8~S*g#F$gYU8H7V(d+3T){dF%FDxk9eQI;B{1_5w^VO=#!A'
    '0Hb~VzE!lVpk%tU2+3u2DwGn!uH-tUTyr*Y%qZ7_?I5$tRaIBXbtbvy@4lJI`R4B7DJfPNWbFd*UJV)ABXZ?Rw$sa2TdKfHtE66D'
    '>gOu3(yG8}9<g4mz^XFWY2|vc0;@`{)5`VY1y+@8r;=@Mnu{`SHD_CA$!yR2v!&3eq&wA+D7AZQdf`fD=A7MMarqLJ>cKmcWSq-G'
    'UQIR5D$qGX;e#TAyM)!66Jy~!hUq*|Sv<v5BMP+=<CGrtg4ku{M#~8FJldsJ+2zz?EZXxty%@t91UK)3%rvpXD8Wu08f-|)TS>aK'
    'q~|c(@%1dge$@-&0<T0o)t-&a`GgWXHQ-sYGkv`MPG`PJhWR8%w2<*(;#h7a)rnG_!(@wUSb(u6L|e&oT6tzq3YC*){z;?qcDjG_'
    'mY@z2EbYwgRuY{{qH|j5I6n3TSgItXtE4)uRP#>km6K@EnZNS3O54XPYhA2d=d{=2dKP4{AtO;G+i7Gwh82E#!A_u-FK@YDHaxqo'
    'Y7JRIMS}B?xf}+w5HqVS^p&v73wsV*o><ia%sH(9m5@sbc|KP>k%E0*tB$xg$*$v6-<4(0FZ4y&_L(~CtRhsRo<`IK#2M3zcMS7J'
    'd7IAR?(U)mCz+vHz0MyHIaz4`m9WbSdk%{pTg?J&IV2dZthkI|=kw5G8FJ>a<QOY<Wy@zJ@_DWKOno!ibLE%It?%d1QI*&yC;2x-'
    '6*+{_CAUAO$($>Yo<XGfY`ZfHHJx;0fnu#}wUl7fJjpZ2wy=sBwzs|(r{-n;sNcs!_S9d`yFBBJ9z?&CPsorUp&Wv+d(OdOpey>Q'
    'd<x22s+^=6JAxK>aichr+2BqYVsSB!45ueaiWpI&oX=bnzDV@V4Z9)vssO+IA-95Yl#_Wy=EyW5$_xz$rw_2e{l<E;vt^rWZL7J_'
    'F)fo%exhY^tz#Kl+iYpZhS{0?MAzChnwx9gjSW-V*lJDFDorz+oi)z2Hg9istjQ#ii{gIFA!S7I#>y4*2%Zs7WGVi~kp52?jYt#0'
    'A2@y>=)>a)BhrM75e*=fqt9mt?>IwxRMPb_At=E{mti_jG7M(Y>fl2HH<P(7j*V6Luioc<En+*VG2~dQh2&>7CJ0ohankO4oCy-N'
    '1FkEICP7lGxFnM-Ov0Au;FQcV!=E#bUx3#kbdWxt@Fs?{mq$*|W|v>zj`w_wVJ5}J%>5jGne#{Qn}fHRo*7T}JFaj!3@)GPJ}bNP'
    '%W)fTz2zZco<3#!$1>t(!-Ux~@Wk{K{_iM`%m(r$2|W*qmL&Fl!RVW-#$W)D6s$tGtonweMWwGx^VnXGJ(|rnA6ZXC2^Viul74bI'
    'AqiTg%R>)-s|bd6jUjG<mfl79A_8y_Pknc17to1_U-7JVbaY!nRhWtF_LU0)MrW67>{*0&(<GUm4g<#LENe}{j6RNF;Ed7^Edk;9'
    'm@r``oZW9Vd?Gg#(!gV>B5R{*jLq7JCq^aRcbE_2Z}(o7yGeGxwBJgnW>W{k?~feo9*y{73Dp2Wh{Yi}-j|ZEg@?!mYJ?~f9X!sW'
    '_yvUKa_P?E=&X%JXRSi##iH{f2F9NwI&0-JTZ}>Hg(hvQ&^e3F+8Lp<Rt}xD3Y|-$vm8<7vOL7_`3e=2hki>lF+8`JJY$BQ(oh+R'
    'ZwbJuXgJ5R!Ng2p1)bHv&SH1=UyEQTJRXZ_VI=)NQMSW?S}D6fRc`M@!kOhBd837kgRR8_@4eB0QEG}@DuKRh^EA)LbD7L*>d<P|'
    'LF+7|<HD?9NtTi21D#pNs0R(`md3$5BKir;vC+@VGD$-cK13vzTUOc#W<3ZKV(hn;I$3rMVYCGyj8-Ac1caG!;5Ew#Z$jR|B}Eu*'
    '_HowdfH2x}BaGnBAayHT#FYFhu_#ecHBgvH*b`@9sQ_kT2s1I?XlXE+n2t1;iDe0Zr634r8Sgn0<Ou4Ku=C2{&lSZ@QwE5x$h_!_'
    '%aOL!_$AZ&aY08jF-A&K&0t)Vpo@x<1Xdt69>h|3Q+^XhadzYs0nIXCRMN@LgGA6dWguwJIA$fl2aAStLZ*w;(4os0fZ{JTn)o2<'
    'fDVhL_p2x1WFL#Ah^pYPOD7DlyTq1sLA((Ulai;2DvX)1jHWPRCUhB-UwsmJ&R9klHkKM>3D7F-QfGoI#3@vx7*^pWp;huC*##k('
    '+WR;L$<z_s5T3$=w_-=Ce<^l$FIh-jm&MX+a0qfm+si_bTw=&O#H5MhFk|ZERRZ$1hePN|0x=0+*WekM6&a;~^~iJW6|hYKY#}_A'
    '2Db@}Gh^9KmH?hIK6)m6QXP~AsYwCVwM^z`2h%dM7n#;*vWE}Z?rr!trhdjB*Y?><Y(xIoe`ODTt*K%%FEf;;e9d?<nXwt~P|oJO'
    '2GAT8ygH%;dTvl^Y-lzXr-UP@kg6O~#e$&Ov1)82RZO~4;Z+*15)srw5Q{*H(#c0C)oLylrs51o%RPdm5uUC{M+!%fRE#AVWR4We'
    'L^WWX4Xv_~QE9*$Ll9I*O0xtYl^F%h1X4MKNK*h59;%dsGZ=A+GyVANp>f37wd*(T?CgCBFHW`n=y@2j#W805JXIQHL;zIb%%X5+'
    'p3rD4h0z$&oFzk>d4i%`+NKI|7LPdctUlM_gL61xVvzHiEE#sHpFE_JB5}ccM{x!a|9lcmoG0{|DnywFney12*mzzbR>Ur|3<J2r'
    'ke=l`<Jpc<gN89>Jt5y*bh?99<3bv%Xpt$k<A&uBCFRU+<I1;K-<wofQlO2%Kf+xJt`~?*EktF2trGV|uBCXLOO1=iC{11TM-d{v'
    'j%8$@HTpKQU5hqwAYq1TdkqBb!B82eq-yeHyFcnZw~^QxoGL!+b8)q24x>dFF0MC}RxSYo=Y=1$txASg2w@x=Z@>pEaEU!aX1=x^'
    'JoMsvCmHq~d=#hAdbF~5e0ak0PpOdoX3>ig=D~yD-<j|l+3gzQCC-32Mt!DgynUsHXuC>Cn~axgaw<DE!6zZa7@RQ0+C$jr=W+Ig'
    '9e;Ackh;V{oe-ua4hagniGVFiH~d2ekQj_SqBsIg>5gdpAnrSO0ML?~_J+=o_nZNL&S87v_SRbY{i|a(jGbjb9D!*{cPdy-z-nYG'
    'rIB@?dNkUzVSsf>gMxU{bNgwMEMbS9J37Fv8(=Uj&yUJ#9VBQ&Ri~4>;m+EKFGx_i(cW3Qoo(%M!=iXA_Tb1L4~g-Tav*?nC@2nY'
    'Scl@im=l4K0Elqd<FxLIgAH^<EaxrHA3Byi4qdx|2s=+OJOer>Rk%Xt*z82wWJ{y-PGs45Y}}VN%)TJ)8-hz^-rWA;_y`k%W_YhG'
    'TQIqrI6p3`lN=ddglrcOi{oUzG3rwy@et$%dECBqJ36d|zv=>Bpp&eg;U)Sphh8~kEWKm+_x6X}68;Y4=cVXG5x2YU$YU|4QpwB*'
    'T0ZK7G|Ur5Jj7IX;Ih#`H9~q($B%rqJ;n{?eU`NGdd{4E&UYmSxiwZ0rqyB|J%JL!VbOd)DQoY9xr1=Mz@yK+q#hK1bg&Ro&_g^`'
    'Lln*#+W{SQgOcApaO-Z*t`7|_Xu&vg<n!@ge$%Da35r;>y|Bl98uH!3ir72OP?0fSy<Ed$L(ieYIIqMMbM+;Pw&G0G$xXNre{qQE'
    'C<?<559KYCB+0>;q>`NS^W_j1x*@28ArFU>Bp(un3w{-;kpg6JqEwBU>G~y9Q|c}(?q|+O3(1l6<tHT=Bc~T3fs2}Wt@oV==`%#x'
    'tk<~i3i<>Bw?&$3@j*3ts31CvB(zuz^Wo>EXc0v{PHSLY!PN`4EmvD8^cu(zIb4q^`E7~-9j7>~Lm5Mj|FWGU4yHh!{cjSwl|T|h'
    '$OS$jR5rL8w?_HK9*?^ae5m9n$jFCI-_%EbazZbs{qe)BqLGU`j8|hrgctN!>|g|5fXHu4eU4CsdThRD{O90wd}fX)Jl}l#>Wf;V'
    '`S9WH?H6&^>cfZE_Fvkdf9$`q_tKiUCAWILNL#=jK1^-^dl9#PsonSTMd{k1QrclAYHc;At+r&>$k3uL+`>c?yxt|mB&vJRLm9Bt'
    '2<eFZ#vi&AF3JE1%Rp$3nkwhjoI!R}BtTJ3G?HROqs?qkL=Yv<Xmt(KBUY~2oM%0id0_n4hi$ASCc6aMWWUOzGx4%|`gDwMb3;h+'
    '(Z10|8}30<z|CSjfx7|y+=!^p0|O!=BbLhn;yU=YzM%t}__EPjP2EFu0gy{1fxvLr8-Wet)(pz%a1^m!#8Nq^XA(7IsYkLk0mMqW'
    '^!lkTEy%$j3x}CDQgiU}aR%T*s{hBxy13rrsm#P1La~;AN#Vpm^n$dOBxU>N0RF!9)>|tzwj#$}C|)__2Mxwmu;2u)tzZ%G+nx)d'
    'u@3)0ZaU#=GIybw;WZe?^oIe|6kZtxhc1k7Nau8bZBGYv7{EhE4vHGewV;><DlP_Cz6Ha2SwVQUPFL$|w$a{dcI<lFY?}3UYu%_D'
    '>&<4p*|NE@-t8K8vxzT7eR2jPLw=QN3%t@6?1HHejb<f41FS=JxobE*P+cAT^#S)FTpI-HmNPhVt$6)#p}t}8O>N!O>YMiZR=vH>'
    'jk?(a@U!eLU+ZqI+nu!zK0I(?7>Y3uJ}B)5uakv{DIERK@TndCp`opByr3Ctw%KYMR>x><w)y4;Z*@&$ecj;Pwl=%gmT791X~XZP'
    ');7(pt>(riTr{>io2_oE$z#<g`kk(YS!Pl%g?F{FPrK%pvDGqL8%<5?T3y>VK@iPNd(GZlvs+smU9;QLIJ~^GZf<Cu^^K0TW_5WR'
    'UmB}>VE15@ZVXA~rl4XLs@a1v8x)}^0WEz6!)&ZMhJAuFedR7(6_6)BP`rf7N;DHys(dv<a_VPY>q2j?t#zArtHU?pk73d$gyk{g'
    'j8XtlO?Tj6G9NTPyt+CZnP4TWhY;bf_MDOL+;>)+YmL(nzxVWmzpR>|d57q%{MBZwar(P|eEQ+{R-5g{XYYOsemwr@y;T|~L<@}~'
    '_*}(iukN>Wt<k;TICw`WpUoe@E7xlB6Q}(WS$F{W0A@g)ap1Y5p?rS7CHksw;x;h4w5O2}6S$A6^9XfEj?HxU7!~*hv?CA+#I}0a'
    '@tC5Bv^*y06?|Bx1X5O@nx3OFp&%z{3qC2b0L9IFe4jk3v1w?;pS31-0Xyu?t@Z9!+umyNjs;=J`O(ByRui=%O)UD788oqVUNteJ'
    'EfX{`w@->RF-3s<tTnN-+1+THYn!IIzP{G!7#nt*pBGKk@-T6|NE3^`WRfOo7YGwG+A={CbNi$~6Vv3~&sr1PYb|TtvUo=`A;j8f'
    'ZES9yBTdY~#CDM;7JbPKns{z8F{3RLG%>eNB27$f!hP1dXLO9tCPcbT4%of5j$@F{Inlj%a-F#LwTiT_=v&I@OIjSWs!Y0`2}+XN'
    'bb5DsS)f2cXnh^uB4^FzltNl(<kgJQ#7d9Rnpi9HR3w~}nO9d#$L&L%I(!@`MR;S?8x8zb54zkLpq5<Tt?%vZZePDyfBohCdb7E{'
    'Dt6SSl|?o??;EnJA%=|sAI7yJ_E33+$civu8PYF_PwRZ1QkN~rlQ>Sa<qJC<LMLPp)i`DU%Liue`{;!^{T{D8f6D7~?)_w2W|zZS'
    'jvO*|PcM<=qw~ln(};X2<-9OEd-!?ED$$PF<<w5gDP8ZBGD<u)vwYGo&yh{?MUO<G9v|R#5!*$$j!g&Fk6N;tfa}rfA(ZIQWj>6}'
    'Ytrm^{=A{R)z!9aquDlFtxl(9Z?v~Ix3reI-s-GvT5C3pTwATR^{v*%##Xc0Hu$>PG`Bdnn-GR=S*=+0iFrf1Ry1#Dz{}Q5yWQ2a'
    'O>?u=+UOV@-)xv2W4&c=ZEPBL$1<B6?X^y;y<yl}TZXl!wM{5(t{F6J3EUBcwYDz$PX)K6xY%_^j@uK{uRafw^SO`0<_)<RFJ}Ve'
    'jktEHq|niAdc1D6+JpMhTG#_p%fxD(-rC&S+S=UgY;2M;C41+<HF{Y1(?9?9$#?(x^f$kF{K<O^+^<~Ge*8bbnMjKG@1K10^qb#c'
    'mmPN(w40s&{N2az{`b@0{oRv){>Rf_d<|kiJixO`%Xv`r>0kco@w*>A{p-7@AOG_4cfS4PM}JR?RZ!`ZkAL>`gZH0&{7<Jp{mK9P'
    '+J6bh6xeHTl#g!9gl+l3by2*CbL*6poXlNaLMxZ4x$(Nn%1z63pMPNO@%tkh;FyL_>t@)1Y$4z6iIoH_Ms18g3>N}##IbW}<qHeY'
    'nuyw>wi4b70jTJ{Ty_y~^@3U_Xm>k?w#D1c7T@Tsb=ziV)9iNJAY-#>;K~7v__x;0wRNkzVT0MVo89*2TBq4Gt<ILwj@2(C!G%|k'
    't47d)Vu%ZBxy+yY2z{i;<W9ox8a<y&TqiHAJgtqeRenNv_6f>-gUy+O$j&Yxz)hU1eN;5PkhJ~#=g;tyGu*jmpDt1veP#5EH~OMg'
    'yxG^s&erI)i*5AUWTV$AqpytqY>mEXabWgo_XQfgwq%}Oo9yYe%F|ayf7V7{v^G8as_)qvy>_vUUYl(6m8Y+aegQ^*m(*Vo$*9Ro'
    'Ic=?WJ?Br)C0D}6tj<llm{u@VpR;;5aoIAm@Wkb_H1q|mcB&2U)VA<yE&^n4M^+v`E2#FZJIOv$v-cGqJTG`aUxaG_CRvlhgh^{q'
    'yobBUjpReQ4ks^uh!$)M7LKP`>zx1#Q><&p{Jp%A0s?h3Bz{^&yu%GbnJ3_<jMV|A5QQ2Z!{U|YJ?EAu)up`Q6&P4%)qUdLfO3oY'
    'lg)4XjSZC{R))BohM3-7F?Qp}^t&`B7~j-8RVu@)3~vbx@2)sH*>#W}K!;JRqxRgqLGKw*WO@Ym?vJ=1#P3OUrK9(v+hbQkXBl3p'
    'la7K$k4Pp@{?Ff?{^oy+-7=Cc<I_L>=JW@@M%Ii~MVi6H&X-UB{F6`r_|fV2e=9b+{QD<A7LPyu!@oZL>$@S_#FHQY+v&G|_4va-'
    '$xSyn2^13ThYu0i{%T0yaQcToKodUu+RsnF{+&<%_^rq9efad(zxeFke-Lk$duoVJhE!>AZvee3Hfni%a3BV?q3Npbq(i4Twbp1N'
    'iu6tAU?g@csgQrbx$WyXYv6PpOKtVa&Me?T-6LMv_w!@l<PFE$l9sTg%dc;*`bMAhc{QosPd@qR@h|`H=?{JnR`K|w?@A5@$(#Tt'
    'fg1mpAAI`BzkmVYo-)w)#~*wXOd(`PjW(i%Ov&u@>)#LA7sirJt<w7{BdUz(92(JD!ic~?p8nTApMK-lPv8G0Zm<-V^5nZe17mpd'
    'lb=2Q;HRPUfudkHIi*j3`0Xd}e-N6+<3IiW>4*OY7axE7XODmUb1;L`pM34f$6tT??!P|%?f(!}$5={mk7%4N0^jt@AAR=2-=BW)'
    'yH7v<`0+oy|Magv1S6IYrg<MKE32&R99mgBX=U&Ii`*go-gln-@}s98e(TA{{|n+QupzN$8pE#t_A78Or{DYS>9;<3^5YL4|M`1h'
    '38&xq+UZZe!D7n;jYb0^zCWVf{^7&NfBE}QKmOHc@BaxNIQ{vb#A6W1o&NMY!pq4>)8>75HE>T>1Bhn*)vogZnQDB!#}5qaM8`WI'
    '9ddlSG4PMM=cBj#>_^{u`h)kL{N}r-?|=RD`|p1G@$V~ZtE}yrTie~lC(x*CrSBiqyIU>Iu)Dn3Su>iOrqNkzk;Wxk&@{N#wmaOk'
    '3~Q~mW;J=ovQ1lq3*25a%q?@%?zGx%yV=wXZnw5NnqhRdHnjEThS}`2vo+5BO*D?gM9d?Tf=73dFPpiWj%%=8Ff7xs?$IU)%e6^G'
    'Zr#|vd2?q^zp{Pv>h-JJcXsr>o&UUZ<<8DkVX~f_F4E1BeZa+uHzG#G85!GAGf6bmr0WbSifHN-8J5wb#4n()2T1;QiAc0XlpL_('
    'q2Y5$S!Qu0%sv<ym=_Cd-et(dL~E@B{s1>NDApumeWpVv8W9Zj@fU4;(5WHvz*I=mh1`n(Z~;Biq5(WMQlz(lOU*~QgZdo$W`~Q@'
    'WK)ngi@uKYK8I&0COjxPooLQ0cGhMk%OYEs^uBT$EPN`0MUnMkbqo{j>i$=*-?#x$*nd!2vKj>+p5TsB?15y7m+>SIahgICERP4A'
    'SO{xOu|CV6^glY&Mx3CC=#C#iyNI7HWL3v$8@Zm343m`9g4tMdU?KCS;>p5EonA;LD%nBFZ8XA^U*OEted2!uWn-e(7*1>|ibTcE'
    'hP1>+&oTT4elAi1Dq6+S5WSnxmwb_dpKPw=wMA7{up9BY#@Ut|>}5f%DH$FmDJ49b>@DmdBVs&#SxWHgWoC|WruR}hnORs7a^lHG'
    '6R}^QQYO(iy^03M2K+c|XYV9>jnnT-uKI{hFYLfBHOU=tOgj=}o77<21I5NC`SkJQUG9rs7VK#6AXTd+EX@!@YvQzKA*VIz#)wjB'
    '8}Q&ERD$~PpTw8)27zbDhfcG7aW?eWc}PE$e*V6btd^)vP~>XV)OXk`Q8D3>^7fOd=>uI6L&xnt#{dbT$KirAz{7n(c|cJQUOYfS'
    'CG8SxFz@!9gTnw-LeZW^YXhL_MrdZJWXuMKgW4;#5cQ$KcF;LR<czTj;&kgwG3H4&RYswEO{T*UnRS?9qp-QSCk+Cb^PpCu!Xy*h'
    '2Rk1G7{fA2k<JuFIst^><0L#gU}AHXh;~`eI00pU8y&8owv3tw#-(cOj(TzT8n&BgPa}IXPcRb#?{(D&bLK}aLjrZESSVADNyU^W'
    'mz-YuNX0U!RE4xO+-B5uc+VEmnivS(eRP(FL)nlT$c)mEGvg(^@tB9l8BjI@(`-hRt>=t9^X2W`&<V@@VPq#Y!RAdVpq_~*CNn7I'
    'ip6AWnoFLVt+<*)Hyr|ho}X^8uW&x379>rA@+et{u0w?4q5x7bQ#O(si?P^dljfyuo!Y$}_k^KKJ@6>MA^?_jEcM6@DiYC?2D@{}'
    '0c%3D47sYweMhcZq*z2O8ccBNDs+JFg8MG@IJsSn31GzG<*>?Mcz2t%tf`P=dbc~~;fsqL$_10b)FPgJf(gJ7;qUx}3x4(<m{NKW'
    'VK%mgT(N(4KKPP~t+5;!mjf`<L$3JH=W^o0aQs8{)(hfUXS5NB&xS~y5^Be6){?E};@Ztsfx4tAsmx&!P>Y`ejRlK)%+E&ZiQY~+'
    '!+2E%o=k95N7+z9)Ti8lJwy2odUrUArQ{7@!r{}>aC?&SS-D?|3m}3pf|#5{R(O@#F}fT=I$Vm<lt<oy2@;d;iAriH$E&JU4R#AB'
    'Z}6^zBV)MUcYJVMVW-!kXd?m>nR!xjbwf8{o`e3#kK1LG7)+REZoVXGg7ibta~FJif@^!Ct>$-8Nc9u*pVFRFemBQXru4nJ4gWk`'
    '?<4ATfxL-ch#^R#bBLV}1#OKy4`&U-2vP2N6V!=ny9@Pm1VK#k<)9`ZM2Am@L^`$DX$FjfhA}|Lr3=g`Ax#OSA*XAV%+yq-u<a3e'
    'rdxZjTz&P*&g$OI_SH*KTopxlQZf8^u;zL;$5Okm-r2shLmTi^Ia^G72t_t|q|20gh0JGvdv_=LFiLL{g~XsW5DJ1Xay`*#%n{0P'
    '2Fhj`&Bb2AxfhU|ObkO4Fvnnh@q|m1d_ft@U7Dgj4uzQ{!b;dg&w)oytX{|luhY;WNxCHh1F@k{YLz-(6VK<O3$&LMR#JoC2zMn$'
    'r*-b;X!;v@s*bIY*FtHn;8NcTwNhy#<(7FdHTtRJpys87QAJq?R$L5##B>2-uqq+~3M>aYCJ@Gt^indL2Cn7y!u|?tS9kOyE&f(%'
    '{iW#=rh)=o^eoN3vY?uC4t_4C=Dc&{GuNEM{xm~fxo6sDuO~X!GFKg$&H1y}mfmeVog;cAEqMrG2|||g<Z@jLuwf8Hq<<?nU)kH;'
    'zM=2jyedw_4FEI2ehgoDQ$KKrV*9d(tEGS`2Ll(EBg9$ZFm~)=G7q(w#0N*bhe<ocQ!9yuX-q^Zjz7zzL@~lanFT@*+<Tm2{MLF~'
    'Yew6<qwCFeZIejh0)U+GvbNr7Z<6zj4W7Uijg-Y3Fy0uFH;!-s`%8pYx{3@k1*2uKXpL26I*oOvO1P-1hmL%kygz-eL9QS^u$RX_'
    '!yB<6Nb<CW-!8VN$JR&)vBM|jUiEZ^k{8gA$Q&~{wnaT^J_WymHYgz`M@eCH<pU*8q+|G81rZYAUzjB~%M0WtOrx)3f<={+B)ULt'
    'JAwhL8Y@8~4<cT~$T6yR3QvglqO@r8UftqG%kH!-b918unBKD2Hq7|5kv2!msKTekonLfjHMGZ6Zkh%4MDExw`DMbZfz1XG6ZB4W'
    '*;SdtgHzy7B=vB^c+?Bo+&s^!3!-IseS7cD^=sQ#?&z=H+<tBQ`i<?&H+Ez{;hLT&cCzF6H($@wMP-}EmYz-k(T-Lfhq!>5o-z&y'
    '`7x=T?u$Wp^ji~epv6qOL<TN13UZwi4B{1tixC$vWt&Lfo4)7|h9WW?>LQ1od{_K}CdNQ=ok)+cB{Nux4}8SXMUOHEr!UG^`w*Z9'
    'lzTWVmCcST$zj1Q`Qf$*7zEsT<;p8J^y@cY+rDxA>Pmd5nKMBlGZACssp142lJ^4m43Cok#|Gt>e518-S3dMc^Dfy7I)pe<APofW'
    'T&~ldde?08&f1n~cRI$pv1PQ@TAQLrtwWSn0t=rd9F)-A@UTb11IY1t0$x+i&b&8<is0RF6;C8?Q=JwQx2yO-<k|d2qM$CH_ZnhM'
    '5;3+;HuFcsIO5ANVdaWSQW=}SQ8OKf2CW%{8w{c`p7@eR+3vC2g8?4Tifyh*j3S<y2FR%vh2wGvO?Sf4u82kiPjToB3QePZS52r%'
    '(}?L-x-(>4`L+x!mbC|Bp|AK7(IvhPzzm=h0{nEng<~i^Fg&q0tQeBQX|VXVv9CrSpqm^9#$35LbkL8cv0aX{_9Cj|o+n#9g+a?|'
    'WBn~GMD_UHXJYhsA3gg2RD{Vy'
)

_EPOCH = "planning-epoch-dfa6c0fd-5831-47c9-a894-7c413500d191"
_BASIS = (
    "write_reproduce_script uses the approved Plan output-path mapping for "
    "plan.bam_list only as literal text, without opening that file's contents. "
    "The full-command obligation remains and must be checked against the actual "
    "approved/executed action records at delivery, including create_bam_list; "
    "no execution receipt or completed output is asserted at planning time."
)


def _recording():
    data = json.loads(zlib.decompress(base64.b85decode(_RECORDED_B85)))
    assert hashlib.sha256(data["candidate1_raw"].encode()).hexdigest() == (
        "4568a60420d994340c2a0130e2285a9eb49bcf22f627f78a4a133b2a70714dae"
    )
    assert hashlib.sha256(data["candidate2_raw"].encode()).hexdigest() == (
        "cc3c61be7f8080f1259990858ba9e8219cf59058d6585ab997aabb56fb69fd89"
    )
    return data


def _candidates():
    data = _recording()
    previous = parse_planner_outcome_envelope(data["candidate1_raw"]).plan_candidate_body()
    deleted_read = parse_planner_outcome_envelope(data["candidate2_raw"]).plan_candidate_body()
    control = deepcopy(previous)
    control["steps"][16]["inputs"].remove("plan.bam_list")
    assert control == deleted_read  # The actual second candidate had no basis.
    assert len(previous["steps"]) == 18 and len(previous["proposed_artifacts"]) == 35
    return data, previous, deleted_read


def _production_dataflow(candidate, data):
    binding = ControllerBinding(**data["binding"])
    task = data["task_spec"]
    skills = ["genomics/wgs-short-read-mapping", "genomics/wgs-gatk-snp-indel"]
    manager = SkillManager(str(Path(node_context.__file__).parents[1] / "skills"))
    capabilities = manager.get_execution_capability_registry(skills)
    handles = {handle: row["stable_identity"] for handle, row in binding.semantic_handle_map.items()
               if row["object_type"] == "artifact"}
    origins = {row["stable_identity"]: "verified_task_input" if row["direction"] == "input" else "not_yet_available"
               for row in binding.semantic_handle_map.values() if row["object_type"] == "artifact"}
    _keys, usage, wire_issues = validate_planner_candidate_deterministically(
        candidate, candidate_scope_id=binding.candidate_scope_id,
        authoritative_handles=handles, availability_origins=origins,
        authoritative_bindings=binding.semantic_handle_map, validate_dataflow=False,
        require_declared_references=True, capability_ids=tuple(capabilities),
    )
    assert not wire_issues, wire_issues
    resources = compile_plan_resource_bindings(
        candidate, candidate_scope_id=binding.candidate_scope_id,
        authoritative_handles=handles, availability_origins=origins,
        authoritative_bindings=binding.semantic_handle_map, require_declared_references=True,
    )
    allocation = prepare_allocation_intent(candidate, candidate_scope_id=binding.candidate_scope_id,
                                         planning_epoch_id=_EPOCH)
    resolved = resolve_plan_proposal_handles(candidate, binding)
    plan = build_plan_state("recorded-planner-consumption", json.dumps(resolved, ensure_ascii=False), strict=True)
    plan.update(artifact_usage=deepcopy(usage), allocation_intent=deepcopy(allocation),
                candidate_scope_id=binding.candidate_scope_id, planning_epoch_id=_EPOCH,
                plan_resource_bindings=deepcopy(resources))
    normalized, _audit, normalization_issues = normalize_plan_contracts(
        plan, renderer_records=[manager.get_renderer_record(skill) for skill in skills if manager.has_renderer(skill)],
        output_contracts=effective_output_contracts(task, project_root="/recorded-project"), task_spec=task,
    )
    assert not normalization_issues, normalization_issues
    compiled, _receipt, effect_issues = compile_plan_step_effects(
        normalized, capabilities, allow_legacy_untyped=False, resource_bindings=resources,
    )
    assert not effect_issues, effect_issues
    _final, _dataflow_receipt, issues = compile_plan_dataflow(
        compiled, capabilities, resource_bindings=resources, compile_effect_contracts=False,
        validate_dependencies=True, allow_legacy_untyped=False,
    )
    return issues


def _review_issues(previous, candidate, data):
    return validate_planner_consumption_correction(
        previous, candidate, data["feedback"]["validation_issues"],
    )


def test_original_unchanged_read_is_still_rejected_by_the_real_dataflow_gate():
    data, previous, _deleted = _candidates()
    issues = _production_dataflow(previous, data)
    assert len(issues) == 1
    issue = issues[0]
    assert issue["code"] == "PLAN_ARTIFACT_UNAVAILABLE"
    assert issue["field_path"] == "$.steps[16].inputs[0]"
    assert issue["resource_id"] == "plan.bam_list"
    assert issue["producer_step_ids"] == ["create_bam_list"]
    assert issue["rejected_sources"][0]["reason"] == "source_step_is_not_an_ancestor"
    # No removal: the new review rule must not replace the existing DAG error.
    assert _review_issues(previous, previous, data) == []


def test_dependency_only_correction_keeps_read_and_original_promises():
    data, previous, _deleted = _candidates()
    fixed = deepcopy(previous)
    fixed["steps"][16]["dependencies"].append("create_bam_list")
    assert _production_dataflow(fixed, data) == []
    assert _review_issues(previous, fixed, data) == []
    assert fixed["steps"][16]["inputs"] == previous["steps"][16]["inputs"]
    assert fixed["steps"][16]["success_criteria"] == previous["steps"][16]["success_criteria"]


def test_actual_second_candidate_removal_requires_review_basis_even_with_dag_pass():
    data, previous, deleted = _candidates()
    assert _production_dataflow(deleted, data) == []
    issues = _review_issues(previous, deleted, data)
    assert len(issues) == 1
    assert issues[0]["code"] == "PLAN_CONSUMPTION_CHANGE_REVIEW_REQUIRED"
    assert issues[0]["field_path"] in {"$.steps[16].inputs", "$.steps[16].success_criteria"}
    assert "plan.bam_list" in json.dumps(issues)
    assert "write_reproduce_script" in json.dumps(issues)


@pytest.mark.parametrize("basis_field", ["success_criteria", "validation_checklist"])
def test_no_read_source_basis_is_reviewable_without_inventing_a_resource(basis_field):
    data, previous, deleted = _candidates()
    if basis_field == "success_criteria":
        deleted["steps"][16]["success_criteria"].append(_BASIS)
    else:
        deleted["validation_checklist"].append({
            "check_id": "review-reproduce-consumption", "description": _BASIS,
        })
    assert _production_dataflow(deleted, data) == []
    assert _review_issues(previous, deleted, data) == []
    assert deleted["proposed_artifacts"] == previous["proposed_artifacts"]
    assert deleted["steps"][16]["success_criteria"][:3] == previous["steps"][16]["success_criteria"]
    assert "command_receipts" not in json.dumps(deleted)
    # The added basis permits original Plan Review; it does not assert that a
    # future reproduction script or execution receipt has been verified.


def test_reusing_unchanged_rationale_does_not_explain_a_new_read_removal():
    data, previous, deleted = _candidates()
    previous["steps"][16]["success_criteria"].append(_BASIS)
    deleted["steps"][16]["success_criteria"].append(_BASIS)
    issues = _review_issues(previous, deleted, data)
    assert [issue["code"] for issue in issues] == ["PLAN_CONSUMPTION_CHANGE_REVIEW_REQUIRED"]


def test_restoring_the_bad_read_is_rejected_despite_non_read_prose():
    data, previous, deleted = _candidates()
    deleted["steps"][16]["success_criteria"].append(_BASIS)
    deleted["steps"][16]["inputs"].insert(0, "plan.bam_list")
    assert _review_issues(previous, deleted, data) == []
    issues = _production_dataflow(deleted, data)
    assert [issue["code"] for issue in issues] == ["PLAN_ARTIFACT_UNAVAILABLE"]


def test_no_previous_rejected_read_does_not_create_an_unrelated_semantic_gate():
    _data, previous, deleted = _candidates()
    assert validate_planner_consumption_correction(previous, deleted, []) == []
