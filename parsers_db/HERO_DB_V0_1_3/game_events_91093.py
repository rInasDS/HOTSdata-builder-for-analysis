#!/usr/bin/env python3
"""Minimal build-91093 replay.game.events decoder for Pyhots evidence probes.

Type definitions are transcribed from Blizzard heroprotocol protocol91093.py.
Only the dependency closure required by game events is included.
"""
from __future__ import annotations
from typing import Any, Dict, List

# type id -> (kind, spec). Field ordinal from upstream structs is irrelevant for
# bit-packed decoding; fields are serialized in declaration order.
T: Dict[int, Any] = {
0: ('int',(0,7)), 1:('int',(0,4)), 2:('int',(0,5)), 3:('int',(0,6)),
4:('int',(0,14)), 5:('int',(0,22)), 6:('int',(0,32)),
7:('choice',((0,2),{0:('m_uint6',3),1:('m_uint14',4),2:('m_uint22',5),3:('m_uint32',6)})),
8:('struct',[('m_userId',2)]), 9:('blob',(0,8)), 10:('int',(0,8)),
12:('int',(0,3)), 13:('bool',None), 20:('blob',(0,7)), 24:('int',(0,2)),
25:('optional',10), 29:('blob',(0,10)), 30:('blob',(0,11)), 34:('blob',(0,12)),
41:('optional',9), 42:('optional',35), 43:('optional',6), 46:('blob',(0,9)),
52:('bitarray',(0,6)), 61:('optional',1), 73:('blob',(0,6)),
77:('array',((0,5),10)),
72:('struct',[('m_name',20)]),
74:('struct',[('m_name',73)]),
75:('struct',[('m_name',73),('m_type',6),('m_data',20)]),
76:('struct',[('m_type',6),('m_name',73),('m_data',34)]),
78:('struct',[('m_signature',77),('m_toonHandle',20)]),
79:('struct',[('m_gameFullyDownloaded',13),('m_developmentCheatsEnabled',13),('m_testCheatsEnabled',13),('m_multiplayerCheatsEnabled',13),('m_syncChecksummingEnabled',13),('m_isMapToMapTransition',13),('m_debugPauseEnabled',13),('m_useGalaxyAsserts',13),('m_platformMac',13),('m_cameraFollow',13),('m_baseBuildNum',6),('m_buildNum',6),('m_versionFlags',6),('m_hotkeyProfile',46)]),
80:('struct',[]),
81:('int',(0,16)),
82:('struct',[('x',81),('y',81)]),
83:('struct',[('m_which',12),('m_target',82)]),
84:('struct',[('m_fileName',30),('m_automatic',13),('m_overwrite',13),('m_name',9),('m_description',29)]),
85:('int',(1,32)),
86:('struct',[('m_sequence',85)]),
87:('null',None),
88:('int',(0,20)),
89:('int',(-2147483648,32)),
90:('struct',[('x',88),('y',88),('z',89)]),
91:('struct',[('m_targetUnitFlags',81),('m_timer',10),('m_tag',6),('m_snapshotUnitLink',81),('m_snapshotControlPlayerId',61),('m_snapshotUpkeepPlayerId',61),('m_snapshotPoint',90)]),
92:('choice',((0,2),{0:('None',87),1:('TargetPoint',90),2:('TargetUnit',91)})),
93:('struct',[('m_target',92),('m_time',89),('m_verb',29),('m_arguments',29)]),
94:('struct',[('m_data',93)]),
95:('int',(0,26)),
96:('struct',[('m_abilLink',81),('m_abilCmdIndex',2),('m_abilCmdData',25)]),
97:('optional',96),
98:('choice',((0,2),{0:('None',87),1:('TargetPoint',90),2:('TargetUnit',91),3:('Data',6)})),
99:('optional',90),
100:('struct',[('m_cmdFlags',95),('m_abil',97),('m_data',98),('m_vector',99),('m_sequence',85),('m_otherUnit',43),('m_unitGroup',43)]),
101:('array',((0,6),2)),
102:('choice',((0,2),{0:('None',87),1:('Mask',52),2:('OneIndices',101),3:('ZeroIndices',101)})),
103:('struct',[('m_unitLink',81),('m_subgroupPriority',10),('m_intraSubgroupPriority',10),('m_count',3)]),
104:('array',((0,6),103)), 105:('array',((0,6),6)),
106:('struct',[('m_subgroupIndex',2),('m_removeMask',102),('m_addSubgroups',104),('m_addUnitTags',105)]),
107:('struct',[('m_controlGroupId',1),('m_delta',106)]),
108:('struct',[('m_controlGroupIndex',1),('m_controlGroupUpdate',12),('m_mask',102)]),
109:('struct',[('m_count',3),('m_subgroupCount',3),('m_activeSubgroupIndex',2),('m_unitTagsChecksum',6),('m_subgroupIndicesChecksum',6),('m_subgroupsChecksum',6)]),
110:('struct',[('m_controlGroupId',1),('m_selectionSyncData',109)]),
111:('struct',[('m_chatMessage',29)]),
112:('struct',[('m_unitTag',6),('m_buttonSlotA',10),('m_buttonSlotB',10)]),
113:('struct',[('m_speed',12)]),
114:('int',(-128,8)),
115:('struct',[('m_delta',114)]),
116:('struct',[('x',89),('y',89)]),
117:('struct',[('m_point',116),('m_unit',6),('m_pingedMinimap',13),('m_option',89)]),
118:('struct',[('m_verb',29),('m_arguments',29)]),
119:('struct',[('m_alliance',6),('m_control',6)]),
120:('struct',[('m_unitTag',6)]),
121:('struct',[('m_unitTag',6),('m_flags',10)]),
122:('struct',[('m_conversationId',89),('m_replyId',89)]),
123:('optional',20),
124:('struct',[('m_gameUserId',1),('m_observe',24),('m_name',9),('m_toonHandle',123),('m_clanTag',41),('m_clanLogo',42)]),
125:('array',((0,5),124)),
126:('int',(0,1)),
127:('struct',[('m_userInfos',125),('m_method',126)]),
128:('struct',[('m_button',81),('m_metaKeyFlags',81)]),
129:('choice',((0,3),{0:('None',87),1:('Checked',13),2:('ValueChanged',6),3:('SelectionChanged',89),4:('TextChanged',30),5:('MouseEvent',128)})),
130:('struct',[('m_controlId',89),('m_eventType',89),('m_eventData',129)]),
131:('struct',[('m_soundHash',6),('m_length',6)]),
132:('array',((0,7),6)),
133:('struct',[('m_soundHash',132),('m_length',132)]),
134:('struct',[('m_syncInfo',133)]),
135:('struct',[('m_queryId',81),('m_lengthMs',6),('m_finishGameLoop',6)]),
136:('struct',[('m_queryId',81),('m_lengthMs',6)]),
137:('struct',[('m_animWaitQueryId',81)]),
138:('struct',[('m_sound',6)]),
139:('struct',[('m_transmissionId',89),('m_thread',6)]),
140:('struct',[('m_transmissionId',89)]),
141:('optional',82), 142:('optional',81), 143:('optional',114),
144:('struct',[('m_target',141),('m_distance',142),('m_pitch',142),('m_yaw',142),('m_reason',143),('m_follow',13)]),
145:('struct',[('m_skipType',126)]),
146:('int',(0,11)),
147:('struct',[('x',146),('y',146)]),
148:('struct',[('m_button',6),('m_down',13),('m_posUI',147),('m_posWorld',90),('m_flags',114)]),
149:('struct',[('m_posUI',147),('m_posWorld',90),('m_flags',114)]),
150:('struct',[('m_achievementLink',81)]),
151:('struct',[('m_hotkey',6),('m_down',13)]),
152:('struct',[('m_abilLink',81),('m_abilCmdIndex',2),('m_state',114)]),
153:('struct',[('m_soundtrack',6)]),
154:('struct',[('m_key',114),('m_flags',114)]),
155:('struct',[('m_error',89),('m_abil',97)]),
156:('int',(0,19)),
157:('struct',[('m_decrementMs',156)]),
158:('struct',[('m_portraitId',89)]),
159:('struct',[('m_functionName',20)]),
160:('struct',[('m_result',89)]),
161:('struct',[('m_gameMenuItemIndex',89)]),
162:('int',(-32768,16)),
163:('struct',[('m_wheelSpin',162),('m_flags',114)]),
164:('struct',[('m_button',81)]),
165:('struct',[('m_cutsceneId',89),('m_bookmarkName',20)]),
166:('struct',[('m_cutsceneId',89)]),
167:('struct',[('m_cutsceneId',89),('m_conversationLine',20),('m_altConversationLine',20)]),
168:('struct',[('m_cutsceneId',89),('m_conversationLine',20)]),
169:('struct',[('m_leaveReason',2)]),
170:('struct',[('m_observe',24),('m_name',9),('m_toonHandle',123),('m_clanTag',41),('m_clanLogo',42),('m_hijack',13),('m_hijackCloneGameUserId',61)]),
171:('optional',85),
172:('struct',[('m_state',24),('m_sequence',171)]),
173:('struct',[('m_sequence',171),('m_target',90)]),
174:('struct',[('m_sequence',171),('m_target',91)]),
175:('struct',[('m_catalog',10),('m_entry',81),('m_field',9),('m_value',9)]),
176:('struct',[('m_index',6)]),
177:('struct',[('m_shown',13)]),
# type 35 only occurs behind optional clan logo. Upstream: fixed 40-byte blob.
35:('blob',(40,0)),
}

EVENT_TYPES = {
5:(80,'SUserFinishedLoadingSyncEvent'),7:(79,'SUserOptionsEvent'),9:(72,'SBankFileEvent'),
10:(74,'SBankSectionEvent'),11:(75,'SBankKeyEvent'),12:(76,'SBankValueEvent'),13:(78,'SBankSignatureEvent'),
14:(83,'SCameraSaveEvent'),21:(84,'SSaveGameEvent'),22:(80,'SSaveGameDoneEvent'),23:(80,'SLoadGameDoneEvent'),
25:(86,'SCommandManagerResetEvent'),26:(94,'SGameCheatEvent'),27:(100,'SCmdEvent'),28:(107,'SSelectionDeltaEvent'),
29:(108,'SControlGroupUpdateEvent'),30:(110,'SSelectionSyncCheckEvent'),32:(111,'STriggerChatMessageEvent'),
33:(112,'SDynamicButtonSwapEvent'),34:(113,'SSetAbsoluteGameSpeedEvent'),35:(115,'SAddAbsoluteGameSpeedEvent'),
36:(117,'STriggerPingEvent'),37:(118,'SBroadcastCheatEvent'),38:(119,'SAllianceEvent'),39:(120,'SUnitClickEvent'),
40:(121,'SUnitHighlightEvent'),41:(122,'STriggerReplySelectedEvent'),43:(127,'SHijackReplayGameEvent'),
44:(80,'STriggerSkippedEvent'),45:(131,'STriggerSoundLengthQueryEvent'),46:(138,'STriggerSoundOffsetEvent'),
47:(139,'STriggerTransmissionOffsetEvent'),48:(140,'STriggerTransmissionCompleteEvent'),49:(144,'SCameraUpdateEvent'),
50:(80,'STriggerAbortMissionEvent'),55:(130,'STriggerDialogControlEvent'),56:(134,'STriggerSoundLengthSyncEvent'),
57:(145,'STriggerConversationSkippedEvent'),58:(148,'STriggerMouseClickedEvent'),59:(149,'STriggerMouseMovedEvent'),
60:(150,'SAchievementAwardedEvent'),61:(151,'STriggerHotkeyPressedEvent'),62:(152,'STriggerTargetModeUpdateEvent'),
64:(153,'STriggerSoundtrackDoneEvent'),66:(154,'STriggerKeyPressedEvent'),67:(159,'STriggerMovieFunctionEvent'),
76:(155,'STriggerCommandErrorEvent'),86:(80,'STriggerMovieStartedEvent'),87:(80,'STriggerMovieFinishedEvent'),
88:(157,'SDecrementGameTimeRemainingEvent'),89:(158,'STriggerPortraitLoadedEvent'),90:(160,'STriggerCustomDialogDismissedEvent'),
91:(161,'STriggerGameMenuItemSelectedEvent'),92:(163,'STriggerMouseWheelEvent'),95:(164,'STriggerButtonPressedEvent'),
96:(80,'STriggerGameCreditsFinishedEvent'),97:(165,'STriggerCutsceneBookmarkFiredEvent'),98:(166,'STriggerCutsceneEndSceneFiredEvent'),
99:(167,'STriggerCutsceneConversationLineEvent'),100:(168,'STriggerCutsceneConversationLineMissingEvent'),
101:(169,'SGameUserLeaveEvent'),102:(170,'SGameUserJoinEvent'),103:(172,'SCommandManagerStateEvent'),
104:(173,'SCmdUpdateTargetPointEvent'),105:(174,'SCmdUpdateTargetUnitEvent'),106:(135,'STriggerAnimLengthQueryByNameEvent'),
107:(136,'STriggerAnimLengthQueryByPropsEvent'),108:(137,'STriggerAnimOffsetEvent'),109:(175,'SCatalogModifyEvent'),
110:(176,'SHeroTalentTreeSelectedEvent'),111:(80,'STriggerProfilerLoggingFinishedEvent'),112:(177,'SHeroTalentTreeSelectionPanelToggledEvent'),
}

class BitBuffer:
    def __init__(self,data:bytes): self.data=data; self.used=0; self.next=0; self.nbits=0
    def done(self): return self.nbits==0 and self.used>=len(self.data)
    def bits(self,n:int)->int:
        out=0; got=0
        while got<n:
            if self.nbits==0:
                if self.used>=len(self.data): raise EOFError('game stream truncated')
                self.next=self.data[self.used]; self.used+=1; self.nbits=8
            k=min(n-got,self.nbits); part=self.next & ((1<<k)-1)
            out |= part << (n-got-k); self.next >>= k; self.nbits-=k; got+=k
        return out
    def align(self): self.nbits=0
    def aligned(self,n:int)->bytes:
        self.align(); out=self.data[self.used:self.used+n]
        if len(out)!=n: raise EOFError('aligned blob truncated')
        self.used+=n; return out
    def used_bits(self): return self.used*8-self.nbits

class Decoder:
    def __init__(self,data:bytes): self.b=BitBuffer(data)
    def inst(self,tid:int)->Any:
        if tid not in T: raise KeyError(f'missing type {tid}')
        kind,spec=T[tid]
        if kind=='int':
            base,bits=spec; return base+self.b.bits(bits)
        if kind=='bool': return bool(self.b.bits(1))
        if kind=='null': return None
        if kind=='blob':
            base,bits=spec; n=base+self.b.bits(bits) if bits else base
            return self.b.aligned(n)
        if kind=='optional': return self.inst(spec) if self.b.bits(1) else None
        if kind=='array':
            (base,bits),child=spec; n=base+self.b.bits(bits)
            return [self.inst(child) for _ in range(n)]
        if kind=='bitarray':
            base,bits=spec; n=base+self.b.bits(bits)
            return {'length':n,'bits':self.b.bits(n) if n else 0}
        if kind=='choice':
            (base,bits),fields=spec; tag=base+self.b.bits(bits)
            if tag not in fields: raise ValueError(f'choice tag {tag} absent type {tid}')
            name,child=fields[tag]; return {name:self.inst(child)}
        if kind=='struct': return {name:self.inst(child) for name,child in spec}
        raise ValueError(kind)

def varuint(v:Dict[str,int])->int: return next(iter(v.values())) if v else 0

def decode(data:bytes)->List[Dict[str,Any]]:
    d=Decoder(data); loop=0; rows=[]
    while not d.b.done():
        start=d.b.used_bits(); loop += varuint(d.inst(7)); user=d.inst(8)['m_userId']; eid=d.inst(0)
        if eid not in EVENT_TYPES: raise ValueError(f'unknown event id {eid} at loop {loop}, bit {d.b.used_bits()}')
        tid,name=EVENT_TYPES[eid]; payload=d.inst(tid); d.b.align()
        rows.append({'loop':loop,'user_id':user,'event_id':eid,'event_name':name,'payload':payload,'bits':d.b.used_bits()-start})
    return rows
