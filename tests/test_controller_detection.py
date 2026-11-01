# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
import unittest
from studio.controllers import SDLController,TYPES

class FakeSDL:
    def __init__(self):
        self.entries=[(b'shanwan Wired Controller',0x2563,0x0119),(b'PS4 Controller',0x054c,0x09cc)]
        self.closed=[]
    def SDL_PumpEvents(self):pass
    def SDL_NumJoysticks(self):return len(self.entries)
    def SDL_IsGameController(self,index):return 1
    def SDL_GameControllerOpen(self,index):return index+1
    def SDL_GameControllerGetVendor(self,handle):return self.entries[handle-1][1]
    def SDL_GameControllerGetProduct(self,handle):return self.entries[handle-1][2]
    def SDL_GameControllerName(self,handle):return self.entries[handle-1][0]
    def SDL_GameControllerClose(self,handle):self.closed.append(handle)

class ControllerDetectionTests(unittest.TestCase):
    def reader(self):
        reader=object.__new__(SDLController);reader.lib=FakeSDL();reader.handle=None;reader.devices=[]
        return reader
    def test_generic_sdl_accepts_detected_shanwan_at_ordinal_zero(self):
        reader=self.reader();names=reader.detect('sdl',0)
        self.assertEqual(names,['shanwan Wired Controller','PS4 Controller'])
        self.assertEqual(reader.handle,1);self.assertEqual(reader.devices[0]['vendor'],0x2563)
    def test_sony_type_does_not_mislabel_third_party_pad(self):
        reader=self.reader();names=reader.detect('ds4',0)
        self.assertEqual(names,['PS4 Controller']);self.assertEqual(reader.handle,2)
        self.assertEqual(reader.devices[0]['name'],'shanwan Wired Controller')
    def test_device_selection_is_filtered_ordinal_and_missing_stays_unselected(self):
        reader=self.reader();reader.detect('sdl',1);self.assertEqual(reader.handle,2)
        reader.detect('sdl',3);self.assertIsNone(reader.handle)
    def test_generic_type_persisted_value_is_available(self):
        self.assertIn('sdl',TYPES.values())
